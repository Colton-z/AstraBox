"""Bound materials precede the real Claude CLI across preparation and recovery."""
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
    get_session,
    poll_until_ready,
    release_agent,
    release_session,
    stream_turn,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_prepared_runtime_activation import (
    _prepared_identity,
    _run_json,
    _sandbox_exec,
    _wait_prepared,
)
from tests.e2e.test_transcript_mirror import _query_mirror_docs

# Needs the fixture provider in the server image and the gate image; see
# tests/e2e/fixtures/runtime_preparation. The live lanes exclude this shape.
pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]

# The image-baked CLI gate built from tests/e2e/fixtures/runtime_preparation.
PREPARATION_IMAGE_ENV = "ASTRABOX_E2E_PREPARATION_IMAGE"

# Settings come from the running server process, not from `docker exec`'s
# environment, which lacks what the launcher injects into `astrabox serve`.
_SERVER_SETTINGS = """
import json, os
from pathlib import Path
serve = None
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        args = proc.joinpath('cmdline').read_bytes().split(b'\\0')
    except OSError:
        continue
    if args[1:2] and args[1].endswith(b'/astrabox') and args[2:3] == [b'serve']:
        serve = proc
        break
assert serve is not None, 'no astrabox serve process'
environment = dict(item.decode().split('=', 1) for item in
    serve.joinpath('environ').read_bytes().split(b'\\0') if b'=' in item)
os.environ.clear()
os.environ.update(environment)
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers import load_entry_point_providers
from astrabox.seams.runtime_preparation import runtime_preparation_provider
load_entry_point_providers()
settings = load_astrabox_settings()
provider = runtime_preparation_provider()
print(json.dumps({
    'volume': settings.sandbox_workspace_volume or '',
    'vault': bool(settings.sandbox_credential_vault_enabled),
    'provider': provider.name if provider is not None else None,
}))
"""

_NATIVE = """
import json, sys
from pathlib import Path
home, workspace = Path(sys.argv[1]), Path(sys.argv[2])
receipts = home / '.runtime-preparation-native.jsonl'
processes = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        args = proc.joinpath('cmdline').read_bytes().split(b'\\0')
        if (b'--input-format', b'stream-json') not in set(zip(args, args[1:])):
            continue
        env = dict(x.split(b'=', 1) for x in proc.joinpath('environ').read_bytes().split(b'\\0') if b'=' in x)
        if env.get(b'HOME', b'').decode() == str(home):
            processes.append(int(proc.name))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
print(json.dumps({
    'receipts': [json.loads(line) for line in receipts.read_text().splitlines()] if receipts.exists() else [],
    'files': [json.loads(path.read_text()) for path in home.glob('.astrabox-runtime-*-agent-token.json')],
    'processes': processes,
    'workspace_materials': [str(p) for p in workspace.rglob('.astrabox-runtime-*')] if workspace.is_dir() else [],
    'transcripts': [p.read_text() for p in home.rglob('*.jsonl') if p != receipts],
}))
"""


def _native(execute: list[str], identity: dict) -> dict:
    # Another account's /proc environ needs ptrace, which the sandbox denies root.
    result = _run_json([
        *execute, "runuser", "-u", identity["linux_user"], "--",
        "python3", "-c", _NATIVE, identity["home_dir"], identity["workspace_dir"],
    ])
    assert isinstance(result, dict)
    assert not result["workspace_materials"], "Credentials entered the user's task workspace"
    return result


def _hold(server: str, control: str, sid: str, enabled: bool) -> None:
    """Make the fixture refuse (``refusals``) or require a user action for ``sid``."""
    subprocess.run([
        "docker", "exec", server, "python", "-c",
        "import json,sys; from pathlib import Path; "
        "Path(f'/data/runtime-preparation-{sys.argv[1]}.json').write_text(json.dumps([sys.argv[2]] if sys.argv[3]=='yes' else []))",
        control, sid, "yes" if enabled else "no",
    ], check=True, timeout=30, capture_output=True)


def _synchronized(server: str, agent_id: str) -> list[dict]:
    receipts = subprocess.run([
        "docker", "exec", server, "sh", "-c",
        "cat /data/runtime-preparation-synchronized.jsonl 2>/dev/null || true",
    ], check=True, timeout=30, capture_output=True, text=True).stdout
    return [row for row in map(json.loads, receipts.splitlines()) if row["agent_id"] == agent_id]


def _send(client: httpx.Client, sid: str, content: str) -> httpx.Response:
    """A send refused before hand-off answers with an error, not an event stream."""
    return client.post(f"/api/v1/sessions/{sid}/ai-stream", json={
        "content": content, "client_message_id": str(uuid.uuid4()),
    }, headers={"Accept": "text/event-stream"}, timeout=60)


def _gate_environment(client: httpx.Client, tenancy: str, image: str) -> str:
    source = environment_with_tenancy(client, tenancy)
    row = next(item for item in data(client.get("/api/v1/admin/environments")) if item["name"] == source)
    name = f"runtime-preparation-{tenancy}"
    data(client.put(f"/api/v1/admin/environments/{name}", json={
        **row, "name": name, "runtime_template_name": image, "enabled": True,
    }))
    return name


@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
def test_session_materials_gate_native_startup_and_preserve_resume(
    e2e_client: httpx.Client, live_test_deadline: float, tenancy: str,
) -> None:
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code", "The gate wraps the Claude CLI"
    image = os.environ[PREPARATION_IMAGE_ENV]
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["volume"] == "", "Recovery must prove database-backed native history"
    assert settings["provider"] == "native-startup-material-fixture"
    principal = data(e2e_client.get("/api/v1/user/current"))["user_id"]
    gate = _gate_environment(e2e_client, tenancy, image)
    suffix = uuid.uuid4().hex[:8]

    def agent(label: str, capacity: int, environment: str = gate) -> str:
        created = data(e2e_client.post("/api/v1/agents", json={
            "name": f"{label} {suffix}", "environment_name": environment,
            "model": profile["model"], "prepared_capacity": capacity,
        }))
        release_agent(created["agent_id"])
        return created["agent_id"]

    def start(agent_id: str, expected_image: str = image) -> tuple[str, dict, list[str]]:
        sid = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))["session_id"]
        release_session(sid)
        poll_until_ready(e2e_client, sid, expected_image=expected_image)
        detail = get_admin_session_detail(e2e_client, sid)
        return sid, detail, _sandbox_exec(detail["sandbox_id"])

    def material(sid: str, aid: str, detail: dict, execute: list[str]) -> dict:
        # READY admits input; the vendor process may still be starting.
        native = _native(execute, detail["runtime_identity"])
        while not (native["processes"] and native["receipts"]) and time.monotonic() < live_test_deadline:
            time.sleep(0.5)
            native = _native(execute, detail["runtime_identity"])
        assert native["processes"] and native["receipts"], "No real native CLI initialized"
        assert not any(item.get("denied") for item in native["receipts"]), (
            "A native CLI started before its Session material existed"
        )
        receipt = native["receipts"][-1]
        assert receipt["session_id"] == sid and receipt["agent_id"] == aid
        assert receipt["principal_id"] == principal
        assert receipt["sandbox_id"] == detail["sandbox_id"]
        assert receipt["mode"] == 0o600 and receipt["phase"] == "startup"
        assert len(native["files"]) == 1
        return native

    # Pools fill while the cold path runs; the budget has no room to wait twice.
    aid = agent("Material warm", 1)
    control = agent("Preparation control", 1, environment_with_tenancy(e2e_client, tenancy)) if settings["vault"] else ""

    # Placement without waiting capacity must meet the same startup requirement.
    cold_agent = agent("Material cold", 0)
    cold_sid, cold, cold_exec = start(cold_agent)
    material(cold_sid, cold_agent, cold, cold_exec)

    waiting = _wait_prepared(e2e_client, aid, live_test_deadline)
    execute = _sandbox_exec(waiting["sandbox_id"])
    receipt = _prepared_identity(aid, tenancy, execute)
    unclaimed = _native(execute, receipt["runtime_identity"])
    assert not unclaimed["receipts"] and not unclaimed["processes"] and not unclaimed["files"], (
        "Unclaimed inventory initialized a CLI or contains personal Session material"
    )
    assert _synchronized(server, aid), "Agent resources were not synchronized before inventory was published"
    sid, original, execute = start(aid)
    assert original["sandbox_id"] == waiting["sandbox_id"], "Conversation cold-created instead of claiming"
    material(sid, aid, original, execute)
    marker = "PREPARED_" + uuid.uuid4().hex
    first = stream_turn(e2e_client, sid, content=f"Remember {marker}. Reply with that exact marker only, without tools.")
    assert first.error is None and marker in first.text, first.error or first.text
    wait_until_settled(e2e_client, sid)
    native = material(sid, aid, original, execute)
    assert native["files"][0]["phase"] == "turn"
    assert native["files"][0]["revision"] > native["receipts"][-1]["revision"]

    # A provider refusal must not enter the supplier's native conversation.
    rejected = "REJECTED_" + uuid.uuid4().hex
    _hold(server, "refusals", sid, True)
    try:
        failed = _send(e2e_client, sid, rejected)
        assert failed.status_code == 502, failed.text
        assert failed.json()["error"]["code"] == "TURN_PREPARATION_FAILED", failed.text
        assert "SECRET_SENTINEL" not in failed.text
        wait_until_settled(e2e_client, sid)
    finally:
        _hold(server, "refusals", sid, False)

    # A prerequisite blocks the send, is visible to its owner, and clears by an explicit operation.
    blocked = "BLOCKED_" + uuid.uuid4().hex
    _hold(server, "user-actions", sid, True)
    try:
        state = data(e2e_client.get(f"/api/v1/sessions/{sid}/user-action"))
        assert state["ready"] is False and state["user_action"]["url"] == "https://example.com/authorize", state
        assert [action["id"] for action in state["actions"]] == ["authorize"], state
        held = _send(e2e_client, sid, blocked)
        assert held.status_code == 409, held.text
        assert held.json()["error"]["code"] == "TURN_PREPARATION_USER_ACTION_REQUIRED", held.text
        wait_until_settled(e2e_client, sid)
        cleared = data(e2e_client.post(f"/api/v1/sessions/{sid}/user-action", json={"action_id": "authorize"}))
        assert cleared == {"ready": True, "user_action": None, "actions": []}, cleared
    finally:
        _hold(server, "user-actions", sid, False)
    transcripts = json.dumps(_native(execute, original["runtime_identity"])["transcripts"])
    assert marker in transcripts, "The native SessionStore scan found no conversation to inspect"
    assert rejected not in transcripts and blocked not in transcripts, "Refused input reached native SessionStore"

    data(e2e_client.post(f"/api/v1/admin/sessions/{sid}/evict-runtime"))
    recall = "What exact marker did I ask you to remember? Reply with only that marker, without tools."
    second = stream_turn(e2e_client, sid, content=recall)
    assert second.error is None and marker in second.text, second.error or second.text
    wait_until_settled(e2e_client, sid)
    attached = get_admin_session_detail(e2e_client, sid)
    assert attached["sandbox_id"] == original["sandbox_id"]
    material(sid, aid, attached, execute)
    while time.monotonic() < live_test_deadline:
        if marker in json.dumps(_query_mirror_docs(sid)):
            break
        time.sleep(0.3)
    assert marker in json.dumps(_query_mirror_docs(sid)), "Native history did not reach database custody"

    replacement = _wait_prepared(e2e_client, aid, live_test_deadline)
    data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
    data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60))
    poll_until_ready(e2e_client, sid, expected_image=image)
    restored = get_admin_session_detail(e2e_client, sid)
    assert restored["sandbox_id"] == replacement["sandbox_id"], "Recovery cold-created instead of claiming"
    assert (restored["sandbox_id"], restored["runtime_identity"]["home_dir"]) != (
        original["sandbox_id"], original["runtime_identity"]["home_dir"]
    ), "Recovery reused the terminated runtime"
    assert restored["engine_session_key"] == attached["engine_session_key"]
    material(sid, aid, restored, _sandbox_exec(restored["sandbox_id"]))
    third = stream_turn(e2e_client, sid, content=recall)
    assert third.error is None and marker in third.text, third.error or third.text
    wait_until_settled(e2e_client, sid)
    assert get_session(e2e_client, sid)["state"] == "READY"

    # The same candidate retains ordinary initialized prewarming under Vault.
    if control:
        ready = _wait_prepared(e2e_client, control, live_test_deadline)
        csid, detail, control_exec = start(control, profile["image"])
        assert detail["sandbox_id"] == ready["sandbox_id"]
        assert not _native(control_exec, detail["runtime_identity"])["files"]
        response = stream_turn(e2e_client, csid, content="Reply with exactly VAULT_OK, without tools.")
        assert response.error is None and "VAULT_OK" in response.text, response.error or response.text
        wait_until_settled(e2e_client, csid)
    print(json.dumps({"tenancy": tenancy, "vault": settings["vault"], "cold": cold["sandbox_id"],
                      "claimed": original["sandbox_id"], "restored": restored["sandbox_id"]}), flush=True)
