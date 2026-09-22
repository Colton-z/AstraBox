"""A displaced shared conversation resumes on an existing SDK-pool box.

The fault removes only this test Agent's resident-box pointer, not the box or
its other live conversation. This distinguishes replacement-box acquisition
from merely reopening an isolated session in the original resident box.
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile,
    data,
    durable_mounts_of_sandbox,
    get_admin_session_detail,
    pod_uid,
    poll_until_agent_ready,
    run_terminal,
    stream_turn,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_prepared_runtime_activation import (
    _run_json,
    _sandbox_exec,
    _supplier_processes,
    _wait_prepared,
)
from tests.e2e.test_shared_box_cohabitation import _create_shared_agent, _start_conversation
from tests.e2e.test_transcript_mirror import _query_mirror_docs

pytestmark = pytest.mark.e2e


def _server_json(program: str, *args: str) -> dict | list:
    return _run_json([
        "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
        "python", "-c", program, *args,
    ])


def _idle_boxes(agent_id: str) -> dict:
    # Read the supplier's shared inventory, not a process-local pool or an
    # Agent-prepared slot (which already belongs to its resident shared box).
    result = _server_json("""
import asyncio, json, sys
from opensandbox.async_redis_pool_store import AsyncRedisPoolStateStore
from redis.asyncio import Redis
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
async def main():
    ensure_database_wiring()
    settings = load_astrabox_settings()
    assert settings.sandbox_workspace_volume, 'persistent-files partition required'
    agent = await AgentRepository().get_agent(sys.argv[1])
    assert agent and agent['agent_id'] == sys.argv[1]
    name = agent.get('_client_pool_name')
    assert name, 'Agent has no SDK client pool'
    redis = Redis.from_url(settings.agent_prewarm_redis_url)
    try:
        store = AsyncRedisPoolStateStore(redis, key_prefix='astrabox:opensandbox:client-pool')
        entries = await store.snapshot_idle_entries(name)
        print(json.dumps({'pool_name': name,
            'sandbox_ids': [entry.sandbox_id for entry in entries]}))
    finally:
        await redis.aclose()
asyncio.run(main())
""", agent_id)
    assert isinstance(result, dict)
    return result


def _agent_workspace_id(agent_id: str) -> str:
    # An Agent-shared box mounts the Agent's durable root, so the workspace
    # identity lives on the Agent row, not on each conversation's Session.
    result = _server_json("""
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
async def main():
    ensure_database_wiring()
    agent = await AgentRepository().get_agent(sys.argv[1])
    assert agent and agent['agent_id'] == sys.argv[1]
    print(json.dumps({'workspace_id': str(agent.get('workspace_id') or '')}))
asyncio.run(main())
""", agent_id)
    assert isinstance(result, dict)
    return result["workspace_id"]


def _detach_resident(agent_id: str, sandbox_id: str, sid: str, sibling: str) -> None:
    # Exact-owner fault injection: the shared box and both native files stay
    # intact; only the Agent's placement hint is lost. The suspended Session's
    # own binding must already have been released by the public terminate API.
    result = _server_json("""
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
async def main():
    ensure_database_wiring()
    agent_id, sandbox_id, sid, sibling = sys.argv[1:]
    sessions = SessionRepository()
    old = await sessions.get_session(sid)
    neighbor = await sessions.get_session(sibling)
    assert old and not old.get('sandbox_id'), 'old Session is still attached'
    assert neighbor and neighbor.get('sandbox_id') == sandbox_id
    repo = AgentRepository()
    agent = await repo.get_agent(agent_id)
    assert agent and agent.get('sandbox_id') == sandbox_id
    changed = await repo.compare_and_update_agent(agent_id,
        expected={'sandbox_id': sandbox_id}, updates={'sandbox_id': None})
    assert changed, 'resident ownership changed before fault injection'
    after = await repo.get_agent(agent_id)
    assert after and after.get('sandbox_id') is None, 'resident pointer was not cleared'
    print(json.dumps({'detached_sandbox_id': sandbox_id}))
asyncio.run(main())
""", agent_id, sandbox_id, sid, sibling)
    assert result == {"detached_sandbox_id": sandbox_id}


def _file_from_physical_box(execute: list[str], identity: dict, filename: str) -> str:
    result = _run_json([
        *execute, "runuser", "-u", identity["linux_user"], "--", "python3", "-c",
        "import json,sys; from pathlib import Path; "
        "print(json.dumps({'text': (Path(sys.argv[1])/sys.argv[2]).read_text()}))",
        identity["workspace_source_dir"], filename,
    ])
    assert isinstance(result, dict)
    return result["text"]


def test_shared_persistent_conversation_resumes_on_existing_sdk_pool_box(
    e2e_client: httpx.Client, live_test_deadline: float, request: pytest.FixtureRequest,
) -> None:
    """Keep files and native history across warm-box replacement, without evicting a neighbor."""
    settings = _server_json(
        "import json; from astrabox.common.utils.settings import load_astrabox_settings; "
        "print(json.dumps({'persistent': bool(load_astrabox_settings().sandbox_workspace_volume)}))",
    )
    assert settings == {"persistent": True}, "Run this case in the persistent-files partition"
    profile = current_profile()
    agent_id = _create_shared_agent(e2e_client, prewarm=True)

    def borrow() -> str:
        waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline)
        sid = _start_conversation(e2e_client, agent_id)
        poll_until_agent_ready(e2e_client, sid)
        assert get_admin_session_detail(e2e_client, sid)["sandbox_id"] == waiting["sandbox_id"]
        return sid

    def remember(sid: str, marker: str) -> None:
        answer = stream_turn(e2e_client, sid, content=(
            f"Our hypothetical project label is {marker}. "
            "Explain revenue growth in one sentence including this label. Do not use tools."
        ))
        assert answer.error is None, answer.error
        assert marker in answer.text, answer.text
        wait_until_settled(e2e_client, sid)

    def recall(sid: str, marker: str, other: str) -> None:
        answer = stream_turn(e2e_client, sid, content=(
            "What was our original project label? Answer from our conversation without tools."
        ))
        assert answer.error is None, answer.error
        assert marker in answer.text, answer.text
        assert other not in answer.text, "The reply used another Session's history"
        wait_until_settled(e2e_client, sid)

    sibling = borrow()
    neighbor_marker = "NEIGHBOR_" + uuid.uuid4().hex
    remember(sibling, neighbor_marker)
    sid = borrow()
    marker = "RESTORED_" + uuid.uuid4().hex
    remember(sid, marker)
    original = get_admin_session_detail(e2e_client, sid)
    neighbor = get_admin_session_detail(e2e_client, sibling)
    original_box = original["sandbox_id"]
    assert neighbor["sandbox_id"] == original_box, "The test requires an actual live co-tenant"
    identity = original["runtime_identity"]
    neighbor_identity = neighbor["runtime_identity"]
    assert original["engine_session_key"]
    workspace_id = _agent_workspace_id(agent_id)
    assert workspace_id, "The shared Agent has no durable workspace root"
    execute_original = _sandbox_exec(original_box)
    neighbor_processes = _supplier_processes(
        execute_original, neighbor_identity, profile["engine_kind"],
    )
    filename = "persistent-warm-resume.txt"
    file_marker = uuid.uuid4().hex
    for session, value in ((sid, file_marker), (sibling, neighbor_marker)):
        out, err, rc, _ = run_terminal(
            e2e_client, session, f"printf %s {shlex.quote(value)} > /workspace/{filename}",
        )
        assert rc == 0, (out, err)
    assert _file_from_physical_box(execute_original, identity, filename) == file_marker
    mirrored: list[dict] = []
    while time.monotonic() < live_test_deadline:
        mirrored = _query_mirror_docs(sid)
        if marker in json.dumps(mirrored):
            break
        time.sleep(0.3)
    assert marker in json.dumps(mirrored), "The native turn has not reached database custody"

    idle: dict = {}
    while time.monotonic() < live_test_deadline:
        idle = _idle_boxes(agent_id)
        if idle["sandbox_ids"]:
            break
        time.sleep(0.3)
    assert len(idle.get("sandbox_ids", [])) == 1, idle
    waiting_box = idle["sandbox_ids"][0]
    assert waiting_box != original_box, "Resident reuse is not SDK replacement-box acquisition"
    execute_waiting = _sandbox_exec(waiting_box)
    waiting_pod = execute_waiting[execute_waiting.index("exec") + 1]
    waiting_pod_uid = pod_uid(
        pod=waiting_pod, kubeconfig=os.environ["ASTRABOX_E2E_KUBECONFIG"],
        namespace=os.environ["ASTRABOX_E2E_KUBE_NAMESPACE"],
    )

    def mounts(execute: list[str]) -> list[dict]:
        return durable_mounts_of_sandbox(
            pod=execute[execute.index("exec") + 1],
            kubeconfig=os.environ["ASTRABOX_E2E_KUBECONFIG"],
            namespace=os.environ["ASTRABOX_E2E_KUBE_NAMESPACE"],
        )

    # Each box mounts its own routed view claim, so equal mount entries show the
    # same workspace layout, not the same backing data. The old-file reads after
    # recovery are the evidence that both views route to the Agent's root.
    original_mounts = mounts(execute_original)
    waiting_mounts = mounts(execute_waiting)
    assert original_mounts and waiting_mounts == original_mounts, (
        "Both physical boxes must already mount the Agent's persistent root at the same path"
    )
    reclaimed = data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
    assert reclaimed["sandbox_id"] == original_box and reclaimed["killed"] is False
    _detach_resident(agent_id, original_box, sid, sibling)
    data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60))
    poll_until_agent_ready(e2e_client, sid)
    resumed = get_admin_session_detail(e2e_client, sid)
    assert resumed["sandbox_id"] == waiting_box, "Recovery did not acquire the observed SDK idle box"
    assert pod_uid(
        pod=waiting_pod, kubeconfig=os.environ["ASTRABOX_E2E_KUBECONFIG"],
        namespace=os.environ["ASTRABOX_E2E_KUBE_NAMESPACE"],
    ) == waiting_pod_uid, "The observed warm Pod was replaced during recovery"
    assert _agent_workspace_id(agent_id) == workspace_id, "Recovery replaced the Agent's workspace root"
    assert resumed["engine_session_key"] == original["engine_session_key"]
    restored_identity = resumed["runtime_identity"]
    for field in ("uid", "gid", "linux_user", "home_dir", "workspace_dir", "workspace_source_dir"):
        assert restored_identity[field] == identity[field], f"Recovery changed filesystem identity: {field}"
    for field in ("isolated_session_id", "terminal_isolated_session_id"):
        assert restored_identity[field] != identity[field], "Old namespaces cannot belong to the new box"
    assert _file_from_physical_box(execute_waiting, restored_identity, filename) == file_marker
    out, err, rc, _ = run_terminal(e2e_client, sid, f"cat /workspace/{filename}")
    assert rc == 0 and out.strip() == file_marker, (out, err)
    recall(sid, marker, neighbor_marker)
    recall(sibling, neighbor_marker, marker)
    neighbor_after = get_admin_session_detail(e2e_client, sibling)
    assert neighbor_after["sandbox_id"] == original_box
    assert neighbor_after["runtime_identity"] == neighbor_identity
    assert neighbor_after["engine_session_key"] == neighbor["engine_session_key"]
    assert _supplier_processes(
        execute_original, neighbor_identity, profile["engine_kind"],
    ) == neighbor_processes
    assert _file_from_physical_box(execute_original, neighbor_identity, filename) == neighbor_marker
    evidence = json.dumps({
        "engine": profile["engine_kind"], "agent_id": agent_id, "session_id": sid,
        "fault": "resident_pointer_loss_after_isolated_release", "pool_name": idle["pool_name"],
        "original_box": original_box, "observed_idle_box": waiting_box,
        "observed_idle_pod_uid": waiting_pod_uid,
        "resumed_box": resumed["sandbox_id"], "workspace_id": workspace_id,
        "workspace_source_dir": restored_identity["workspace_source_dir"],
        "native_session_key": resumed["engine_session_key"], "sibling_session": sibling,
        "sibling_processes": neighbor_processes, "persistent_mounts": waiting_mounts,
    })
    request.node.user_properties.append(("shared_persistent_warm_resume", evidence))
    print(evidence, flush=True)
