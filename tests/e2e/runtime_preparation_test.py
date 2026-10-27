"""Bound materials precede the real Claude CLI across preparation and recovery.

An Assistant's prepared resources are synchronized in its own box before
Hermes runs them, and its workspace is ready before its first conversation.
A message reaches the runtime, and its reply the console, without waiting on
the platform, and each message is prepared once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    OPEN_AGENTS,
    OPEN_ASSISTANTS,
    Reply,
    SessionStream,
    current_profile,
    data,
    environment_with_tenancy,
    follow_stream,
    get_admin_session_detail,
    get_session,
    history,
    message_text,
    output_position,
    poll_until_ready,
    psql,
    psql_rows,
    release_agent,
    release_assistant,
    release_session,
    send_and_follow,
    send_input,
    sql_literal,
    transcript_entries,
    user_messages,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_prepared_runtime_activation import (
    _prepared_identity,
    _run_json,
    _sandbox_exec,
    _supplier_processes,
    _wait_prepared,
    _wait_prepared_count,
)

# Needs the fixture provider in the server image and the gate image; see
# tests/e2e/fixtures/runtime_preparation. The live lanes exclude this shape.
pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]

# The image-baked CLI gate built from tests/e2e/fixtures/runtime_preparation.
PREPARATION_IMAGE_ENV = "ASTRABOX_E2E_PREPARATION_IMAGE"
# Skills at a fixed commit, so a change of the Assistant is a change of its Skills.
_SKILLS = "https://github.com/anthropics/skills.git@34040c9c568585f6929bedeaad110ad08f079624"


def test_provider_state_commit_is_fenced_by_current_subject(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Provider rows commit with current subjects on the caller's actual transaction."""
    client = e2e_client
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code", profile["engine_kind"]
    assistant_environment = os.environ["ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"]
    assistant_model = os.environ["ASTRABOX_E2E_ASSISTANT_MODEL"]
    assert psql("SELECT count(*) FROM assistant_defaults WHERE scope = 'deployment'") == ["0"], (
        "this exclusive fixture requires absent defaults; do not delete shared defaults"
    )
    nonce = uuid.uuid4().hex
    path = f"/api/v1/runtime-preparation-fixture/guards/{nonce}"
    principal = data(client.get("/api/v1/user/current"))["user_id"]
    runtime_profiles = data(client.get("/api/v1/admin/runtime-profiles"))
    source_name = environment_with_tenancy(client, "agent")
    source = next(row for row in runtime_profiles if row["name"] == source_name)
    environment = f"guard-{nonce}"
    data(client.put(f"/api/v1/admin/runtime-profiles/{environment}", json={**source, "name": environment}))
    agent_fields = {
        "name": f"Guard {nonce}", "runtime_profile_name": environment,
        "model": profile["model"], "prepared_capacity": 0,
    }
    created_agent = data(client.post("/api/v1/agents", json=agent_fields))
    agent = created_agent["agent_id"]
    release_agent(agent)
    assistant = data(client.post("/api/v1/assistants", json={
        "display_name": f"Guard {nonce}", "runtime_profile_name": assistant_environment,
        "model_config_override": {"model_name": assistant_model},
    }, timeout=max(1, live_test_deadline - time.monotonic())))["assistant_id"]
    release_assistant(assistant)
    _assistant_workspace(client, assistant, live_test_deadline)
    assert psql("SELECT count(*) FROM assistant_defaults WHERE scope = 'deployment'") == ["0"]

    def capture(kind: str, subject_id: str, name: str) -> None:
        data(client.post(f"{path}/capture/{kind}/{subject_id}/{name}"))

    def rows() -> list[dict]:
        return psql_rows(
            "SELECT subject_kind, subject_id, marker FROM runtime_preparation_fixture_state "
            f"WHERE nonce = {sql_literal(nonce)}", order_by="marker",
        )

    def refused(name: str, marker: str) -> None:
        response = client.post(f"{path}/commit/{name}/{marker}")
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert {key: error[key] for key in ("code", "status_code", "category", "owner", "retryable")} == {
            "code": "PREPARATION_SUBJECT_CONFLICT", "status_code": 409,
            "category": "state", "owner": "platform", "retryable": False,
        }, error
        assert marker not in [row["marker"] for row in rows()], rows()

    def held(marker: str) -> int:
        while time.monotonic() < live_test_deadline:
            current = data(client.get(path))["held"]
            if marker in current:
                return int(current[marker])
            time.sleep(0.2)
        pytest.fail(f"guarded transaction never held marker {marker}")

    def operation(kind: str, subject_id: str) -> dict:
        if kind == "agent":
            query = (
                "SELECT a.created_by AS owner, a.version, r.runtime_generation, r.client_pool_epoch, "
                "r.resources_operation_provider AS provider, r.resources_operation_id AS operation_id, "
                "r.resources_operation_deadline > extract(epoch FROM clock_timestamp()) AS unexpired "
                "FROM agents a JOIN agent_runtimes r USING (agent_id) "
                f"WHERE a.agent_id = {sql_literal(subject_id)} AND a.state = 'ACTIVE'"
            )
        else:
            query = (
                "SELECT a.owner_id AS owner, w.current_sandbox_id, "
                "w.prepared_operation_provider AS provider, w.prepared_operation_id AS operation_id, "
                "w.prepared_operation_deadline > clock_timestamp() AS unexpired "
                "FROM assistants a JOIN assistant_workspaces w USING (assistant_id) "
                f"WHERE a.assistant_id = {sql_literal(subject_id)} AND NOT a.deleted"
            )
        result = psql_rows(query)
        assert len(result) == 1, result
        return result[0]

    def settled(kind: str, subject_id: str) -> None:
        while time.monotonic() < live_test_deadline:
            current = operation(kind, subject_id)
            if current["operation_id"] is None:
                return
            time.sleep(0.2)
        pytest.fail(f"{kind} synchronization did not settle: {current}")

    opened = False
    passed = False
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            with httpx.Client(base_url=str(client.base_url), timeout=30) as anonymous:
                response = anonymous.post(f"{path}/capture/agent/{agent}/anonymous")
                assert response.status_code == 401, response.text
            data(client.put(f"/api/v1/admin/runtime-profiles/{environment}", json={
                **source, "name": environment, "enabled": False,
            }))
            capture("agent", agent, "agent-old")
            opened = True
            data(client.post(f"{path}/commit/agent-old/agent-disabled"))
            assert [row["marker"] for row in rows()] == ["agent-disabled"], rows()
            agent_fields["description"] = nonce
            updated_agent = data(client.put(f"/api/v1/agents/{agent}", json={
                **agent_fields, "version": created_agent["version"],
            }))
            refused("agent-old", "stale-agent")
            data(client.put(f"/api/v1/admin/runtime-profiles/{environment}", json={**source, "name": environment}))
            print("guard: disabled RuntimeProfile accepted; stale Agent guard refused", flush=True)

            capture("assistant", assistant, "assistant-old")
            write = pool.submit(
                client.post, f"{path}/commit/assistant-old/assistant-atomic", params={"hold": True},
                timeout=max(1, live_test_deadline - time.monotonic()),
            )
            pid = held("assistant-atomic")
            assert "assistant-atomic" not in [row["marker"] for row in rows()], rows()
            defaults = pool.submit(
                client.put, "/api/v1/admin/assistant-defaults", json={"instructions": nonce},
                timeout=max(1, live_test_deadline - time.monotonic()),
            )
            blocked: list[str] = []
            while time.monotonic() < live_test_deadline:
                blocked = psql(
                    "SELECT pid::text FROM pg_stat_activity "
                    f"WHERE {pid} = ANY(pg_blocking_pids(pid)) AND query LIKE '%installation%'"
                )
                if blocked:
                    break
                assert not defaults.done(), "first defaults writer overtook the guarded transaction"
                time.sleep(0.2)
            assert blocked and not defaults.done(), f"defaults writer never blocked behind guard backend {pid}"
            data(client.post(f"{path}/release/assistant-atomic"))
            data(write.result(timeout=max(1, live_test_deadline - time.monotonic())))
            data(defaults.result(timeout=max(1, live_test_deadline - time.monotonic())))
            assert psql("SELECT instructions FROM assistant_defaults WHERE scope = 'deployment'") == [nonce]
            assert [row["marker"] for row in rows()].count("assistant-atomic") == 1, rows()
            refused("assistant-old", "stale-defaults")
            capture("assistant", assistant, "assistant-fresh")
            data(client.post(f"{path}/commit/assistant-fresh/assistant-fresh"))
            print("guard: uncommitted row invisible; first defaults insert blocked then committed", flush=True)

            for kind, subject_id in (("agent", agent), ("assistant", assistant)):
                data(client.post(f"{path}/arm/{kind}/{subject_id}"))
                mutation = pool.submit(
                    client.put if kind == "agent" else client.patch,
                    f"/api/v1/{'agents' if kind == 'agent' else 'assistants'}/{subject_id}",
                    json={**agent_fields, "prepared_capacity": 1, "version": updated_agent["version"]}
                    if kind == "agent" else {"system": nonce},
                    timeout=max(1, live_test_deadline - time.monotonic()),
                )
                marker = f"callback-{kind}"
                held(marker)
                native = operation(kind, subject_id)
                assert native["owner"] == principal, native
                assert native["provider"] == "native-startup-material-fixture", native
                assert native["operation_id"] and native["unexpired"] is True, native
                if kind == "agent":
                    assert native["runtime_generation"] and native["client_pool_epoch"], native
                else:
                    assert native["current_sandbox_id"], native
                assert marker not in [row["marker"] for row in rows()], rows()
                data(client.post(f"{path}/release/{marker}"))
                data(mutation.result(timeout=max(1, live_test_deadline - time.monotonic())))
                if kind == "agent":
                    prepared = _wait_prepared(client, subject_id, live_test_deadline)
                    assert prepared["runtime_generation"] == native["runtime_generation"], (prepared, native)
                    execute = _sandbox_exec(prepared["sandbox_id"])
                    receipt = _prepared_identity(subject_id, "agent", execute)
                    _supplier_processes(execute, receipt["runtime_identity"], "claude_code")
                else:
                    workspace = _assistant_workspace(client, subject_id, live_test_deadline)
                    assert workspace["current_sandbox_id"] == native["current_sandbox_id"], (workspace, native)
                settled(kind, subject_id)
                refused(marker, f"late-{kind}")
                capture(kind, subject_id, f"final-{kind}")
                data(client.post(f"{path}/commit/final-{kind}/settled-{kind}"))
                print(f"guard: real {kind} callback committed; settled operation cannot write again", flush=True)

            assert [row["marker"] for row in rows()] == sorted([
                "agent-disabled", "assistant-atomic", "assistant-fresh", "callback-agent",
                "callback-assistant", "settled-agent", "settled-assistant",
            ]), rows()
            deletion = pool.submit(
                client.delete, f"/api/v1/agents/{agent}",
                timeout=max(1, live_test_deadline - time.monotonic()),
            )
            while time.monotonic() < live_test_deadline:
                state = psql(f"SELECT state FROM agents WHERE agent_id = {sql_literal(agent)}")
                if state in (["DELETING"], ["DELETED"]):
                    break
                time.sleep(0.2)
            assert state in (["DELETING"], ["DELETED"]), state
            refused("final-agent", "deleted-agent")
            data(deletion.result(timeout=max(1, live_test_deadline - time.monotonic())))
            assert psql(f"SELECT state FROM agents WHERE agent_id = {sql_literal(agent)}") == ["DELETED"]
            OPEN_AGENTS.remove(agent)
            data(client.delete(f"/api/v1/assistants/{assistant}", timeout=max(1, live_test_deadline - time.monotonic())))
            assert psql(f"SELECT deleted FROM assistants WHERE assistant_id = {sql_literal(assistant)}") == ["t"]
            OPEN_ASSISTANTS.remove(assistant)
            refused("final-assistant", "deleted-assistant")
            print(json.dumps({"guard_receipts": rows(), "first_defaults_blocked_backend": blocked}), flush=True)
            passed = True
        finally:
            if opened:
                data(client.post(f"{path}/release/all"))
                if passed:
                    data(client.delete(path))
                    data(client.delete(f"/api/v1/admin/runtime-profiles/{environment}"))

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


def _synchronized(server: str, kind: str, subject_id: str) -> list[dict]:
    receipts = subprocess.run([
        "docker", "exec", server, "sh", "-c",
        "cat /data/runtime-preparation-synchronized.jsonl 2>/dev/null || true",
    ], check=True, timeout=30, capture_output=True, text=True).stdout
    subject = {"kind": kind, "id": subject_id}
    # The file outlives deployments; rows another fixture version wrote name no subject.
    return [row for row in map(json.loads, receipts.splitlines()) if row.get("subject") == subject]


def _refused(
    client: httpx.Client, sid: str, content: str, code: str, *, timeout: float | None = None
) -> dict:
    """Send a message the deployment's preparation must refuse; return the refusal.

    The platform accepts the message, then its reader asks the deployment to
    prepare it before the runtime sees it. A refusal reaches the console as
    ``data-input-failed`` and stays on the message in history; no response
    starts for it.
    """
    reply = send_and_follow(client, sid, content, until="end", allow_refusal=True, timeout=timeout)
    failure = reply.input_failure
    assert failure is not None, (
        f"the message was answered instead of refused by preparation: {reply.response}"
    )
    assert failure.get("code") == code, f"expected {code}: {failure}"
    assert reply.response is None, f"a refused message started a response: {reply.response}"
    stored = [m for m in user_messages(history(client, sid)) if m["id"] == reply.client_message_id]
    assert len(stored) == 1, f"history does not hold the refused message once: {stored}"
    assert (stored[0]["metadata"].get("state"), (stored[0]["metadata"].get("failure") or {}).get("code")) == (
        "failed", code,
    ), stored[0]["metadata"]
    return failure


def _transcript_text(sid: str) -> str:
    return json.dumps(transcript_entries(sid))


def _gate_environment(client: httpx.Client, tenancy: str, image: str) -> str:
    source = environment_with_tenancy(client, tenancy)
    row = next(item for item in data(client.get("/api/v1/admin/runtime-profiles")) if item["name"] == source)
    name = f"runtime-preparation-{tenancy}"
    data(client.put(f"/api/v1/admin/runtime-profiles/{name}", json={
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
            "name": f"{label} {suffix}", "runtime_profile_name": environment,
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
        assert receipt["session_id"] == sid and receipt["subject"] == {"kind": "agent", "id": aid}
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
    assert _synchronized(server, "agent", aid), "Agent resources were not synchronized before inventory was published"
    sid, original, execute = start(aid)
    assert original["sandbox_id"] == waiting["sandbox_id"], "Conversation cold-created instead of claiming"
    material(sid, aid, original, execute)
    marker = "PREPARED_" + uuid.uuid4().hex
    first = send_and_follow(
        e2e_client, sid, f"Remember {marker}. Reply with that exact marker only, without tools.", until="end"
    )
    assert first.error is None and marker in first.text, first.error or first.text
    wait_until_settled(e2e_client, sid)
    native = material(sid, aid, original, execute)
    assert native["files"][0]["phase"] == "turn"
    assert native["files"][0]["revision"] > native["receipts"][-1]["revision"]

    # A provider refusal must not enter the supplier's native conversation.
    rejected = "REJECTED_" + uuid.uuid4().hex
    _hold(server, "refusals", sid, True)
    try:
        failure = _refused(e2e_client, sid, rejected, "TURN_PREPARATION_FAILED")
        assert "SECRET_SENTINEL" not in json.dumps(failure), failure
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
        failure = _refused(e2e_client, sid, blocked, "TURN_PREPARATION_USER_ACTION_REQUIRED")
        assert (failure.get("user_action") or {}).get("url") == "https://example.com/authorize", failure
        wait_until_settled(e2e_client, sid)
        cleared = data(e2e_client.post(f"/api/v1/sessions/{sid}/user-action", json={"action_id": "authorize"}))
        assert cleared == {"applicable": True, "ready": True, "user_action": None, "actions": []}, cleared
    finally:
        _hold(server, "user-actions", sid, False)
    transcripts = json.dumps(_native(execute, original["runtime_identity"])["transcripts"])
    assert marker in transcripts, "The native SessionStore scan found no conversation to inspect"
    assert rejected not in transcripts and blocked not in transcripts, "Refused input reached native SessionStore"

    data(e2e_client.post(f"/api/v1/admin/sessions/{sid}/evict-runtime"))
    recall = "What exact marker did I ask you to remember? Reply with only that marker, without tools."
    second = send_and_follow(e2e_client, sid, recall, until="end")
    assert second.error is None and marker in second.text, second.error or second.text
    wait_until_settled(e2e_client, sid)
    attached = get_admin_session_detail(e2e_client, sid)
    assert attached["sandbox_id"] == original["sandbox_id"]
    material(sid, aid, attached, execute)
    while time.monotonic() < live_test_deadline:
        if marker in _transcript_text(sid):
            break
        time.sleep(0.3)
    custody = _transcript_text(sid)
    assert marker in custody, "Native history did not reach database custody"
    assert rejected not in custody and blocked not in custody, "Refused input reached database custody"

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
    third = send_and_follow(e2e_client, sid, recall, until="end")
    assert third.error is None and marker in third.text, third.error or third.text
    wait_until_settled(e2e_client, sid)
    assert get_session(e2e_client, sid)["state"] == "READY"

    # The same candidate retains ordinary initialized prewarming under Vault.
    if control:
        ready = _wait_prepared(e2e_client, control, live_test_deadline)
        csid, detail, control_exec = start(control, profile["image"])
        assert detail["sandbox_id"] == ready["sandbox_id"]
        assert not _native(control_exec, detail["runtime_identity"])["files"]
        response = send_and_follow(
            e2e_client, csid, "Reply with exactly VAULT_OK, without tools.", until="end"
        )
        assert response.error is None and "VAULT_OK" in response.text, response.error or response.text
        wait_until_settled(e2e_client, csid)
    print(json.dumps({"tenancy": tenancy, "vault": settings["vault"], "cold": cold["sandbox_id"],
                      "claimed": original["sandbox_id"], "restored": restored["sandbox_id"]}), flush=True)


@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
def test_an_agent_without_prepared_capacity_declares_its_resources_before_it_runs(
    e2e_client: httpx.Client, tenancy: str,
) -> None:
    """A cold start synchronizes the Skills it placed before the engine runs them.

    The deployment starts this Agent's Sessions only on resources synchronized
    for its version, and without prepared capacity no inventory does that.
    """
    profile = current_profile()
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Declared {uuid.uuid4().hex[:8]}",
        "runtime_profile_name": environment_with_tenancy(e2e_client, tenancy),
        "model": profile["model"], "prepared_capacity": 0,
        "skills": [f"{_SKILLS}#skills/skill-creator"],
    }))
    aid = created["agent_id"]
    release_agent(aid)
    sid = data(e2e_client.post(f"/api/v1/agents/{aid}/conversations", json={}))["session_id"]
    release_session(sid)
    poll_until_ready(e2e_client, sid)
    receipts = _synchronized(server, "agent", aid)
    assert [row["revision"] for row in receipts] == [str(created["version"])], receipts
    prepared = receipts[0]["prepared_paths"]
    assert prepared and all(prepared.values()), (
        f"the Agent's Skills were synchronized before they were in its box: {receipts}"
    )
    marker = "DECLARED_" + uuid.uuid4().hex
    reply = send_and_follow(e2e_client, sid, f"Reply with exactly {marker}, without tools.", until="end")
    assert reply.error is None and marker in reply.text, reply.error or reply.text
    wait_until_settled(e2e_client, sid)
    print(json.dumps({"tenancy": tenancy, "session": sid, "synchronized": receipts}), flush=True)


def test_a_shared_box_burst_starts_each_conversation_on_its_own_material(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Conversations claiming seats of one shared box at once each get their own material.

    A session-bound Agent's seats wait in a shared box with nothing personal in
    them. A burst claims them together: every seat's engine starts only after
    its own Session's material is in its own runtime home, and each
    conversation's turns refresh only its own material.
    """
    capacity = 3
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code", "The gate wraps the Claude CLI"
    image = os.environ[PREPARATION_IMAGE_ENV]
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    principal = data(e2e_client.get("/api/v1/user/current"))["user_id"]
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Material burst {uuid.uuid4().hex[:8]}",
        "runtime_profile_name": _gate_environment(e2e_client, "agent", image),
        "model": profile["model"], "prepared_capacity": capacity,
    }))
    aid = created["agent_id"]
    release_agent(aid)
    prepared = _wait_prepared_count(e2e_client, aid, capacity, live_test_deadline)
    assert prepared["placement"] == "shared_slot", prepared

    def start(_: int) -> str:
        sid = data(e2e_client.post(f"/api/v1/agents/{aid}/conversations", json={}))["session_id"]
        release_session(sid)
        poll_until_ready(e2e_client, sid, expected_image=image)
        return sid

    with ThreadPoolExecutor(max_workers=capacity) as executor:
        sessions = list(executor.map(start, range(capacity)))
    details = {sid: get_admin_session_detail(e2e_client, sid) for sid in sessions}
    after = data(e2e_client.get(f"/api/v1/agents/{aid}/prepared-runtime"))
    assert after["last_miss_at"] is None, f"a conversation of the burst started cold: {after}"
    seats = {detail["runtime_identity"]["isolated_session_id"] for detail in details.values()}
    assert all(seats) and len(seats) == capacity, seats

    def own_material(sid: str, phase: str) -> None:
        detail = details[sid]
        execute = _sandbox_exec(detail["sandbox_id"])
        native = _native(execute, detail["runtime_identity"])
        while not (native["processes"] and native["receipts"]) and time.monotonic() < live_test_deadline:
            time.sleep(0.5)
            native = _native(execute, detail["runtime_identity"])
        assert native["processes"] and native["receipts"], f"No real native CLI initialized for {sid}"
        assert not any(item.get("denied") for item in native["receipts"]), (
            f"A native CLI started before Session {sid}'s material existed"
        )
        for receipt in native["receipts"]:
            assert (receipt["session_id"], receipt["principal_id"], receipt["sandbox_id"]) == (
                sid, principal, detail["sandbox_id"],
            ), receipt
        assert [item["session_id"] for item in native["files"]] == [sid], native["files"]
        assert native["files"][0]["phase"] == phase, native["files"]

    for sid in sessions:
        own_material(sid, "startup")

    # Each conversation's messages refresh only its own material.
    markers = {sid: "SEAT_" + uuid.uuid4().hex for sid in sessions}

    def answer(sid: str) -> None:
        reply = send_and_follow(
            e2e_client, sid, f"Reply with exactly {markers[sid]}, without tools.", until="end"
        )
        assert reply.error is None and markers[sid] in reply.text, reply.error or reply.text
        wait_until_settled(e2e_client, sid)

    with ThreadPoolExecutor(max_workers=capacity) as executor:
        list(executor.map(answer, sessions))
    for sid in sessions:
        own_material(sid, "turn")
    print(json.dumps({
        "burst": capacity, "vault": settings["vault"],
        "sandboxes": sorted({detail["sandbox_id"] for detail in details.values()}),
        "seats": sorted(seats),
    }), flush=True)


def test_a_startup_user_action_keeps_the_conversation_until_the_user_acts(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """A user prerequisite blocks allocation while history stays readable.

    Known blocked console inputs are refused before history changes. Explicit
    authorization then starts the native runtime, and ordinary later input
    does not query the prerequisite provider again.
    """
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code", "The gate wraps the Claude CLI"
    image = os.environ[PREPARATION_IMAGE_ENV]
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Material action {uuid.uuid4().hex[:8]}",
        "runtime_profile_name": _gate_environment(e2e_client, "conversation", image),
        "model": profile["model"], "prepared_capacity": 0,
    }))
    aid = created["agent_id"]
    release_agent(aid)
    _hold(server, "startup-user-actions", aid, True)
    try:
        namespace = os.environ["ASTRABOX_E2E_KUBE_NAMESPACE"]
        kubectl = ["kubectl", "--kubeconfig", os.environ["ASTRABOX_E2E_KUBECONFIG"]]
        resource = f"/apis/sandbox.opensandbox.io/v1alpha1/namespaces/{namespace}/batchsandboxes"
        inventory = _run_json([*kubectl, "get", "--raw", resource])
        assert isinstance(inventory, dict), "Native sandbox list unavailable"
        version = inventory["metadata"]["resourceVersion"]
        assert version, "Native collection version unavailable"

        def native_events() -> list[str]:
            # Replay from before startup, including boxes already deleted.
            # A failed/expired watch is an observation failure, never a pass.
            observed = subprocess.run([
                *kubectl, "get", "--raw",
                f"{resource}?watch=1&resourceVersion={version}&timeoutSeconds=1",
            ], check=True, capture_output=True, text=True, timeout=30)
            events = [json.loads(line) for line in observed.stdout.splitlines() if line.strip()]
            assert all(event["type"] != "ERROR" for event in events), "Native sandbox watch failed"
            return [event["type"] for event in events
                    if event["object"].get("metadata", {}).get("labels", {}).get("astrabox.session-id") == sid]

        sid = data(e2e_client.post(f"/api/v1/agents/{aid}/conversations", json={}))["session_id"]
        release_session(sid)
        session = get_session(e2e_client, sid)
        while session.get("state") == "CREATING":
            assert time.monotonic() < live_test_deadline, "the conversation never left CREATING"
            time.sleep(0.5)
            session = get_session(e2e_client, sid)
        assert session.get("state") == "READY", f"a startup prerequisite ended the conversation: {session}"
        assert not session.get("sandbox_id") and session.get("runtime_unavailable"), session
        stored = psql_rows(f"SELECT startup_user_action FROM sessions WHERE session_id={sql_literal(sid)}")[0]
        assert stored["startup_user_action"]["url"] == "https://example.com/authorize", stored
        state = data(e2e_client.get(f"/api/v1/sessions/{sid}/user-action"))
        assert state["ready"] is False and state["user_action"]["url"] == "https://example.com/authorize", state

        imported_text = "IMPORTED_WHILE_BLOCKED_" + uuid.uuid4().hex
        imported = data(e2e_client.post(f"/api/v1/admin/sessions/{sid}/history-import", json={"messages": [{
            "source_id": "blocked-history", "role": "user", "created_at": "2026-01-05T09:00:00Z",
            "blocks": [{"type": "text", "text": imported_text}],
        }]}))
        assert imported["imported"] == 1, imported
        assert imported_text in [message_text(message) for message in history(e2e_client, sid)]
        refused_recovery = e2e_client.post(f"/api/v1/sessions/{sid}/recover", json={})
        assert refused_recovery.status_code == 409, refused_recovery.text
        assert refused_recovery.json()["error"]["code"] == "TURN_PREPARATION_USER_ACTION_REQUIRED"

        def counts() -> dict:
            return psql_rows("SELECT " + ", ".join(
                f"(SELECT count(*) FROM {table} WHERE session_id={sql_literal(sid)}) AS {name}"
                for table, name in (("session_inputs", "inputs"), ("session_responses", "responses"),
                                    ("session_output", "output"))
            ))[0]

        def provider_calls(name: str) -> int:
            result = _run_json(["docker", "exec", server, "python", "-c",
                "import json,sys; from pathlib import Path; "
                "p=Path('/data/runtime-preparation-'+sys.argv[1]+'.jsonl'); "
                "print(json.dumps(sum(json.loads(line)['session_id']==sys.argv[2] "
                "for line in (p.read_text().splitlines() if p.exists() else []))))", name, sid])
            assert isinstance(result, int), result
            return result

        before_history, before_counts = history(e2e_client, sid), counts()
        assert before_counts == {"inputs": 1, "responses": 0, "output": 0}, before_counts
        before_operations = provider_calls("user-action-calls")
        payload = {"content": "BLOCKED_" + uuid.uuid4().hex, "client_message_id": str(uuid.uuid4())}
        attachment = {"content": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==",
        }}], "client_message_id": str(uuid.uuid4())}
        for body in (payload, payload, attachment):
            refused = e2e_client.post(f"/api/v1/sessions/{sid}/inputs", json=body)
            assert refused.status_code == 409, refused.text
            error = refused.json()["error"]
            assert error["code"] == "TURN_PREPARATION_USER_ACTION_REQUIRED", error
            assert error["evidence"]["user_action"]["url"] == "https://example.com/authorize", error
            assert history(e2e_client, sid) == before_history and counts() == before_counts
        _hold(server, "user-action-read-failures", sid, True)
        try:
            failed_read = e2e_client.post(f"/api/v1/sessions/{sid}/inputs", json=payload)
            assert failed_read.status_code == 502, failed_read.text
            assert failed_read.json()["error"]["code"] == "SESSION_USER_ACTION_FAILED"
            assert "SECRET_SENTINEL" not in failed_read.text
            assert history(e2e_client, sid) == before_history and counts() == before_counts
        finally:
            _hold(server, "user-action-read-failures", sid, False)
        assert provider_calls("user-action-calls") == before_operations
        assert get_session(e2e_client, sid)["state"] == "READY"
        assert native_events() == [], "A blocked startup created, changed or deleted a native sandbox"
        print(json.dumps({"phase": "blocked-console-refused", "counts_before": before_counts,
                          "counts_after": counts(), "implicit_authorization_operations": 0}))

        cleared = data(e2e_client.post(f"/api/v1/sessions/{sid}/user-action", json={"action_id": "authorize"}))
        assert cleared == {"applicable": True, "ready": True, "user_action": None, "actions": []}, cleared
        assert provider_calls("user-action-calls") == before_operations + 1
    finally:
        _hold(server, "startup-user-actions", aid, False)

    marker = "AUTHORIZED_" + uuid.uuid4().hex
    reply = send_and_follow(e2e_client, sid, f"Reply with exactly {marker}, without tools.", until="end")
    assert reply.error is None and marker in reply.text, reply.error or reply.text
    wait_until_settled(e2e_client, sid)
    detail = get_admin_session_detail(e2e_client, sid)
    native = _native(_sandbox_exec(detail["sandbox_id"]), detail["runtime_identity"])
    assert native["receipts"] and not any(item.get("denied") for item in native["receipts"]), native
    assert native["receipts"][-1]["session_id"] == sid and native["receipts"][-1]["phase"] == "startup"
    assert native_events(), "Native watch did not observe the authorized allocation"
    steady_reads = provider_calls("user-action-reads")
    marker = "ORDINARY_" + uuid.uuid4().hex
    reply = send_and_follow(e2e_client, sid, f"Reply with exactly {marker}, without tools.", until="end")
    assert reply.error is None and marker in reply.text, reply.error or reply.text
    wait_until_settled(e2e_client, sid)
    assert provider_calls("user-action-reads") == steady_reads
    print(json.dumps({"phase": "authorized-native-replies", "prerequisite_reads_before_ordinary": steady_reads,
                      "prerequisite_reads_after_ordinary": provider_calls("user-action-reads")}))


def _assistant_workspace(client: httpx.Client, assistant_id: str, deadline: float) -> dict:
    """The Assistant as its owner reads it, once its workspace is READY."""
    while True:
        assistant = data(client.get(f"/api/v1/assistants/{assistant_id}"))
        if assistant.get("workspace_state") == "READY" and assistant.get("current_sandbox_id"):
            return assistant
        assert assistant.get("workspace_state") in {"NOT_MATERIALIZED", "MATERIALIZING"}, assistant
        assert time.monotonic() < deadline, f"the Assistant's workspace never became READY: {assistant}"
        time.sleep(1.0)


def _profile_revision(assistant_id: str, sandbox_id: str) -> str | None:
    """The revision the profile running in ``sandbox_id`` was prepared from."""
    rows = psql(
        "SELECT configuration_revision FROM assistant_profiles "
        f"WHERE assistant_id = {sql_literal(assistant_id)} "
        f"AND sandbox_id = {sql_literal(sandbox_id)}"
    )
    return next((str(row).strip() for row in rows if str(row).strip()), None)


def _cpu_request(sandbox_id: str) -> str:
    execute = _sandbox_exec(sandbox_id)
    pod = execute[execute.index("exec") + 1]
    spec = _run_json([*execute[:execute.index("exec")], "get", "pod", pod, "-o", "json"])
    assert isinstance(spec, dict)
    container = next(item for item in spec["spec"]["containers"] if item["name"] == "sandbox")
    return str(container["resources"]["requests"]["cpu"])


def _timed_conversation(client: httpx.Client, assistant_id: str, sandbox_id: str) -> dict:
    started = time.monotonic()
    sid = data(client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={}))["session_id"]
    release_session(sid)
    poll_until_ready(client, sid)
    ready = time.monotonic() - started
    assert get_session(client, sid).get("sandbox_id") == sandbox_id, "the conversation is not on the workspace box"
    marker = "PREPARED_" + uuid.uuid4().hex[:12]
    started = time.monotonic()
    reply = send_and_follow(client, sid, f"Reply with exactly {marker}. Do not use tools.", until="end")
    answered = time.monotonic() - started
    assert reply.error is None and marker in reply.text, reply.error or reply.text
    wait_until_settled(client, sid)
    return {"session_id": sid, "ready_s": round(ready, 1), "answer_s": round(answered, 1)}


def test_an_assistant_is_prepared_before_its_conversations(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Creating or changing an Assistant prepares its workspace without a conversation.

    Its Skills are in the box when the deployment synchronizes them, and that
    precedes the backend. A conversation opened afterwards attaches to the
    ready workspace and is answered without waiting for the backend to load.
    """
    environment = os.getenv("ASTRABOX_E2E_ASSISTANT_ENVIRONMENT", "").strip()
    model = os.getenv("ASTRABOX_E2E_ASSISTANT_MODEL", "").strip()
    assert environment and model, "set ASTRABOX_E2E_ASSISTANT_ENVIRONMENT and ASTRABOX_E2E_ASSISTANT_MODEL"
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    principal = data(e2e_client.get("/api/v1/user/current"))["user_id"]
    skills = [f"{_SKILLS}#skills/skill-creator"]
    created = data(e2e_client.post("/api/v1/assistants", json={
        "display_name": f"prepared-{uuid.uuid4().hex[:6]}",
        "runtime_profile_name": environment,
        "model_config_override": {"model_name": model},
        "skills": skills,
    }))
    aid = created["assistant_id"]
    release_assistant(aid)

    def synchronized(revision_other_than: str | None) -> dict:
        while True:
            for row in reversed(_synchronized(server, "assistant", aid)):
                if row["revision"] != revision_other_than:
                    return row
            assert time.monotonic() < live_test_deadline, "the Assistant's resources were never synchronized"
            time.sleep(1.0)

    sandbox_id = _assistant_workspace(e2e_client, aid, live_test_deadline)["current_sandbox_id"]
    assert _cpu_request(sandbox_id) in {"100m", "0.1"}, "the Assistant box does not use its own resource envelope"
    first = synchronized(None)
    assert first["owner_user_id"] == principal, first
    assert first["prepared_paths"] and all(first["prepared_paths"].values()), (
        f"the Assistant's Skills were synchronized before they were in its box: {first}"
    )
    assert _profile_revision(aid, sandbox_id) == first["revision"], "the workspace runs another revision"
    opened = _timed_conversation(e2e_client, aid, sandbox_id)

    data(e2e_client.patch(f"/api/v1/assistants/{aid}", json={"skills": [*skills, f"{_SKILLS}#skills/pdf"]}))
    changed = synchronized(first["revision"])
    assert changed["prepared_paths"] and all(changed["prepared_paths"].values()), changed
    while _profile_revision(aid, sandbox_id) != changed["revision"]:
        assert time.monotonic() < live_test_deadline, "the changed Assistant was not prepared in its workspace"
        time.sleep(1.0)
    reopened = _timed_conversation(e2e_client, aid, sandbox_id)
    print(json.dumps({"assistant": aid, "sandbox": sandbox_id, "first": opened, "after_edit": reopened}), flush=True)
    for timing in (opened, reopened):
        assert timing["ready_s"] < 10, f"the conversation waited for its workspace to be prepared: {timing}"
        assert timing["answer_s"] < 15, f"the first answer waited for the backend to load: {timing}"


def _set_turn_delay(server: str, seconds: float | None) -> None:
    subprocess.run([
        "docker", "exec", server, "python", "-c",
        "import sys; from pathlib import Path; p = Path('/data/runtime-preparation-turn-delay.json'); "
        "p.write_text(sys.argv[1]) if sys.argv[1] != 'off' else p.unlink(missing_ok=True)",
        "off" if seconds is None else str(seconds),
    ], check=True, timeout=30, capture_output=True)


def _turn_preparations(server: str, sid: str) -> list[dict]:
    receipts = subprocess.run([
        "docker", "exec", server, "sh", "-c",
        "cat /data/runtime-preparation-turns.jsonl 2>/dev/null || true",
    ], check=True, timeout=30, capture_output=True, text=True).stdout
    return [row for row in map(json.loads, receipts.splitlines()) if row.get("session_id") == sid]


def test_an_accepted_message_keeps_its_box_running_during_preparation(
    e2e_client: httpx.Client,
) -> None:
    """An idle sweep cannot take compute from a message waiting for preparation."""
    profile = current_profile()
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    source = environment_with_tenancy(e2e_client, "conversation")
    environment = next(row for row in data(e2e_client.get("/api/v1/admin/runtime-profiles")) if row["name"] == source)
    assert "••••••••" not in json.dumps(environment), "The test RuntimeProfile must use deployment model access"
    name = f"runtime-preparation-idle-{source}"
    data(e2e_client.put(f"/api/v1/admin/runtime-profiles/{name}", json={
        **environment, "name": name, "idle_action": "pause", "enabled": True,
    }))
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Preparing input {uuid.uuid4().hex[:8]}", "runtime_profile_name": name,
        "model": profile["model"], "prepared_capacity": 0, "idle_hibernate_seconds": 3600,
    }))
    aid = agent["agent_id"]
    release_agent(aid)
    sid = data(e2e_client.post(f"/api/v1/agents/{aid}/conversations", json={}))["session_id"]
    release_session(sid)
    poll_until_ready(e2e_client, sid)
    warm = send_and_follow(e2e_client, sid, "Reply with exactly WARM. Do not use tools.", until="end")
    assert warm.error is None and "WARM" in warm.text, warm.error or warm.text
    wait_until_settled(e2e_client, sid)
    box = get_admin_session_detail(e2e_client, sid)["sandbox_id"]
    stream = SessionStream(sid, cursor=output_position(e2e_client, sid))
    try:
        _set_turn_delay(server, 20)
        receipt = send_input(e2e_client, sid, "Reply with exactly PREPARED. Do not use tools.")
        iid = sql_literal(receipt["input_id"])
        current_agent = data(e2e_client.get(f"/api/v1/agents/{aid}"))
        data(e2e_client.put(f"/api/v1/agents/{aid}", json={
            "name": current_agent["name"],
            "model": current_agent["model"],
            "runtime_profile_name": current_agent["runtime_profile_name"],
            "version": current_agent["version"],
            "idle_hibernate_seconds": 1,
        }))
        time.sleep(2)  # The accepted input outlives the real configured idle window.
        assert psql(f"SELECT state FROM session_inputs WHERE input_id={iid}") == ["pending"]
        assert all(row.get("turn_id") != receipt["input_id"] for row in _turn_preparations(server, sid)), (
            "Preparation must still be waiting when the sweep runs"
        )
        inspected = False
        inspection_started = time.time()
        inspection_deadline = time.monotonic() + 10
        while not inspected and time.monotonic() < inspection_deadline:
            data(e2e_client.post("/api/v1/admin/sandbox-idle-sweep", timeout=120))
            inspected = (
                f"inputs await admission session={sid} sandbox={box}"
                in _server_log(server, inspection_started)
            )
        assert inspected, "The sweep never inspected this input while preparation was pending"
        assert psql(f"SELECT sandbox_parked_at IS NULL FROM sessions WHERE session_id={sql_literal(sid)}") == ["t"]
        _sandbox_exec(box)  # Independent supplier Pod observation requires Running.
        assert psql(f"SELECT state FROM session_inputs WHERE input_id={iid}") == ["pending"]
        def answered(current: SessionStream) -> bool:
            response = current.answering(receipt["input_id"])
            return response is not None and response.ended

        follow_stream(e2e_client, stream, answered, describe="the message held through the idle sweep")
        response = stream.answering(receipt["input_id"])
        assert response is not None and response.error is None and response.finish_reason == "stop", response
        assert "PREPARED" in response.text, response
        assert get_admin_session_detail(e2e_client, sid)["sandbox_id"] == box
    finally:
        _set_turn_delay(server, None)


#: What the platform may add on a local database, each way. A reply held back
#: until it finished would exceed the outbound budget by the reply's length.
_INBOUND_BUDGET_S = 0.5
_OUTBOUND_BUDGET_S = 0.25

#: The server's own stamps for the two delays (``api/routes/turns.py`` and
#: ``session_runtime/reader.py``): an input accepted by the route, the same
#: input handed to the runtime, and the first text of a response read from it.
_RECEIVED = re.compile(r"input received session=(?P<sid>\S+) at=(?P<at>\d+(?:\.\d+)?)")
_DELIVERED = re.compile(
    r"input delivered session=(?P<sid>\S+) input=(?P<input>\S+) at=(?P<at>\d+(?:\.\d+)?)"
)
_FIRST_TEXT = re.compile(
    r"first text read session=(?P<sid>\S+) response=(?P<response>\S+) at=(?P<at>\d+(?:\.\d+)?)"
)


def _server_log(server: str, since: float) -> str:
    """The server's log lines since ``since`` (epoch seconds)."""
    logs = subprocess.run(
        ["docker", "logs", "--since", str(int(since) - 1), server],
        check=True, timeout=60, capture_output=True, text=True,
    )
    return logs.stdout + logs.stderr


def _console_reply(client: httpx.Client, sid: str, content: str) -> tuple[Reply, float]:
    """Send one message the way the console does and read its reply.

    The console follows the session's stream while it is idle and sends
    through ``POST …/inputs``; the reply arrives on that open stream. Returns
    the reply and the wall time the platform's receipt came back.
    """
    stream = SessionStream(sid, cursor=output_position(client, sid))
    sent: dict = {}
    failures: list[BaseException] = []

    def answered(current: SessionStream) -> bool:
        input_id = sent.get("input_id")
        if input_id is None:
            return False
        if input_id in current.input_failures:
            return True
        response = current.answering(input_id)
        return response is not None and response.ended

    def follow() -> None:
        try:
            with httpx.Client(base_url=client.base_url, headers=client.headers) as reader:
                follow_stream(reader, stream, answered, describe="the console's reply")
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test's thread
            failures.append(exc)

    follower = threading.Thread(target=follow)
    follower.start()
    deadline = time.monotonic() + 30
    while stream.connections == 0 and follower.is_alive():
        assert time.monotonic() < deadline, "the console's stream did not open"
        time.sleep(0.05)
    time.sleep(1.0)  # the console's stream is open long before a message is typed
    sent_wall = time.time()
    receipt = send_input(client, sid, content)
    receipt_wall = time.time()
    sent["input_id"] = str(receipt["input_id"])
    follower.join(170)
    assert not follower.is_alive(), "the reply did not finish"
    if failures:
        raise failures[0]
    reply = Reply(
        session_id=sid,
        input_id=sent["input_id"],
        client_message_id=str(receipt["client_message_id"]),
        stream=stream,
        sent_wall=sent_wall,
    )
    assert reply.input_failure is None, f"the message was refused: {reply.input_failure}"
    assert reply.error is None and reply.finish_reason == "stop", reply.response
    assert reply.response is not None and reply.response.first_text_wall is not None, (
        f"the reply carried no text: {reply.response}"
    )
    return reply, receipt_wall


def _model_header_gate(server: str, marker: str | None) -> None:
    program = """
import json, sys
from model_header_gate import CONFIG, CONTROL
assert CONFIG.is_file(), 'the latency deployment needs the model-header gate on the shipped edge path'
value = json.load(sys.stdin)
temporary = CONTROL.with_suffix('.tmp')
temporary.write_text(json.dumps(None if value is None else {'marker': value, 'seconds': 65}))
temporary.replace(CONTROL)
"""
    subprocess.run(
        ["docker", "exec", "-i", server, "python", "-c", program],
        input=json.dumps(marker), text=True, capture_output=True, check=True, timeout=30,
    )


def _model_header_receipts(server: str, marker: str) -> dict:
    program = """
import json, sys
from model_header_gate import CONFIG, RECEIPTS
marker = sys.argv[1]
rows = [json.loads(line) for line in RECEIPTS.read_text().splitlines()]
print(json.dumps({
    'events': [row for row in rows if row['marker'] == marker],
    'edge': json.loads(CONFIG.read_text())['edge_container'],
}))
"""
    rows = _run_json(["docker", "exec", server, "python", "-c", program, marker])
    assert isinstance(rows, dict)
    return rows


def test_a_delivered_message_is_prepared_once_and_streams_while_it_is_answered(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """A message reaches the runtime, and its reply the console, without waiting on the platform.

    Two delays are the platform's. Inbound: from the route accepting a console
    message (``input received``) to the session's reader handing it to the
    runtime (``input delivered``). Outbound: from the reader reading a
    response's first text from the runtime (``first text read``) to that text
    reaching the console's open stream. Each message is prepared once, before
    the runtime receives it. The first text reaching the console within
    moments of reaching the platform is also what shows the reply streams:
    held back until the end, it would wait out the reply.

    One real model response is silent for 65 seconds before its headers reach
    the shipped sandbox edge. Its request receipt and native store must show
    one attempt and one input; an SDK retry must not hide an edge 504.
    """
    profile = current_profile()
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json(["docker", "exec", server, "python", "-c", _SERVER_SETTINGS])
    assert isinstance(settings, dict)
    assert settings["provider"] == "native-startup-material-fixture"
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Streaming {uuid.uuid4().hex[:8]}",
        "runtime_profile_name": environment_with_tenancy(e2e_client, "conversation"),
        "model": profile["model"], "prepared_capacity": 0,
    }))
    release_agent(created["agent_id"])
    sid = data(e2e_client.post(f"/api/v1/agents/{created['agent_id']}/conversations", json={}))["session_id"]
    release_session(sid)
    poll_until_ready(e2e_client, sid)
    warm = send_and_follow(e2e_client, sid, "Reply with exactly WARM. Do not use tools.", until="end")
    assert warm.error is None and "WARM" in warm.text, warm.error or warm.text
    wait_until_settled(e2e_client, sid)

    _set_turn_delay(server, None)
    measured_from = time.time()
    replies: list[tuple[Reply, float]] = []
    marker = "MODEL_IDLE_" + uuid.uuid4().hex
    _model_header_gate(server, marker)
    try:
        for index, topic in enumerate(("why the sky is blue", "how tides work", "why leaves change colour")):
            context = f" Context label: {marker}." if index == 0 else ""
            replies.append(_console_reply(e2e_client, sid, (
                f"Write about 150 words explaining {topic}.{context} Plain prose, no lists, no tools."
            )))
            wait_until_settled(e2e_client, sid)
            if index == 0:
                _model_header_gate(server, None)
    finally:
        _model_header_gate(server, None)
    log = _server_log(server, measured_from)

    proxy_evidence = _model_header_receipts(server, marker)
    gateway = proxy_evidence["events"]
    attempts = [row for row in gateway if row["phase"] == "received"]
    headers = [row for row in gateway if row["phase"] == "headers"]
    assert len(attempts) == len(headers) == 1, f"the silent model response was retried: {gateway}"
    edge = _run_json(["docker", "inspect", proxy_evidence["edge"]])[0]
    addresses = {network["IPAddress"] for network in edge["NetworkSettings"]["Networks"].values()}
    assert attempts[0]["peer"] in addresses, "the native model request must traverse the actual nginx edge"
    assert headers[0]["status"] == 200 and headers[0]["quiet_seconds"] >= 65, gateway
    assert not [row for row in gateway if row["phase"] == "failed"], gateway
    native = [row["entry"] for row in transcript_entries(sid) if not row["subpath"]]
    users = [index for index, entry in enumerate(native)
             if entry.get("type") == "user" and marker in json.dumps(entry.get("message", {}).get("content"))]
    assert len(users) == 1, "the same delayed input must occur once in the native store"
    end = next((index for index in range(users[0] + 1, len(native))
                if native[index].get("type") == "user"), len(native))
    native_text = "".join(
        part["text"] for entry in native[users[0] + 1:end] if entry.get("type") == "assistant"
        for part in entry["message"]["content"] if part.get("type") == "text"
    )
    assert native_text.strip() == replies[0][0].text.strip(), "the complete native reply must reach the stream"
    retained = [message for message in history(e2e_client, sid)
                if message.get("id") == replies[0][0].response_id]
    assert len(retained) == 1, "the delayed native reply must be retained once in history"
    assert message_text(retained[0]).strip() == native_text.strip(), "history must retain the complete native reply"
    assert retained[0]["metadata"]["status"] == "ended"
    assert retained[0]["metadata"]["outcome"] == "completed"
    print(json.dumps({"model_proxy": {
        "receipts": gateway, "edge": edge["Id"], "native_input_count": len(users),
        "retained_response": retained[0]["id"],
        "native_reply_sha256": hashlib.sha256(native_text.encode()).hexdigest(),
    }}), flush=True)

    received = [float(m["at"]) for m in _RECEIVED.finditer(log) if m["sid"] == sid]
    inbound: list[float] = []
    outbound: list[float] = []
    for reply, receipt_wall in replies:
        # The route's stamp carries no input id: it is the one stamp this
        # session's route wrote while this message's request was open.
        accepted = [at for at in received if reply.sent_wall - 0.05 <= at <= receipt_wall + 0.05]
        assert len(accepted) == 1, (
            f"expected one 'input received' stamp for input {reply.input_id}, found {accepted}"
        )
        delivered = [
            float(m["at"]) for m in _DELIVERED.finditer(log)
            if m["sid"] == sid and m["input"] == reply.input_id
        ]
        assert len(delivered) == 1, (
            f"input {reply.input_id} was handed to the runtime {len(delivered)} times"
        )
        first_read = [
            float(m["at"]) for m in _FIRST_TEXT.finditer(log)
            if m["sid"] == sid and m["response"] == reply.response_id
        ]
        assert len(first_read) == 1, (
            f"response {reply.response_id} has {len(first_read)} 'first text read' stamps"
        )
        inbound.append(round(delivered[0] - accepted[0], 3))
        outbound.append(round(reply.response.first_text_wall - first_read[0], 3))

    # The preparation seam names what it prepares ``turn_id``; the reader passes the input id.
    preparations = _turn_preparations(server, sid)
    prepared = {reply.input_id: [row for row in preparations if row.get("turn_id") == reply.input_id]
                for reply, _ in replies}
    timing = {
        "messages": len(replies),
        "preparations": {input_id: len(rows) for input_id, rows in prepared.items()},
        "received_to_delivered_s": inbound,
        "median_received_to_delivered_s": statistics.median(inbound),
        "first_text_read_to_console_s": outbound,
        "median_first_text_read_to_console_s": statistics.median(outbound),
        "sent_to_receipt_s": [round(wall - reply.sent_wall, 3) for reply, wall in replies],
        "streaming_s": [
            round(reply.response.ended_wall - reply.response.first_text_wall, 2) for reply, _ in replies
        ],
        "session": sid,
    }
    print(json.dumps({"streaming": timing}), flush=True)
    assert all(len(rows) == 1 for rows in prepared.values()), (
        f"a message was not prepared exactly once: {timing}"
    )
    assert timing["median_received_to_delivered_s"] < _INBOUND_BUDGET_S, (
        f"a message waited on the platform: {timing}"
    )
    assert timing["median_first_text_read_to_console_s"] < _OUTBOUND_BUDGET_S, (
        f"a reply waited on the platform: {timing}"
    )
