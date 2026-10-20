"""Concurrent Assistant tools retain their own context across host and box loss."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    data, destroy_box, download, durable_mounts_of_sandbox, get_admin_session_detail,
    history, kube_access, poll_until_ready, psql_rows, release_assistant,
    release_session, send_and_follow, sql_literal, wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.conftest import _wait_health
from tests.e2e.runtime_preparation_test import _assistant_workspace
from tests.e2e.test_conversation_end import _wake_assistant
from tests.e2e.test_durable_workspace import _assistant_snapshot
from tests.e2e.test_prepared_runtime_activation import _run_json, _sandbox_exec
from tests.e2e.test_reattach import _restart_server_container

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live, pytest.mark.backend_restart]

_CONTEXT_KEYS = ["RUNTIME_IDENTITY_SESSION_ID", "ASTRABOX_SESSION_URL", "ASTRABOX_CFUSE_AGENT_TOKEN_FILE"]
_PROBE_SOURCE = """
from pathlib import Path
import json,os,sys,time
prefix=Path(__file__).with_suffix('')
index=int(sys.argv[1]);phase=sys.argv[2]
def path(suffix):return prefix.with_name(prefix.name+'-'+phase+'-'+suffix)
path('start-'+str(index)).write_text('started')
deadline=time.monotonic()+15
while not path('start-'+str(1-index)).exists():
 assert time.monotonic()<deadline, 'the sibling native tool did not run concurrently'
 time.sleep(.05)
token=Path(os.environ['ASTRABOX_CFUSE_AGENT_TOKEN_FILE'])
result={'session_id':os.environ['RUNTIME_IDENTITY_SESSION_ID'],
 'url':os.environ['ASTRABOX_SESSION_URL'],'material':json.loads(token.read_text()),
 'uid':os.getuid(),'file_uid':token.stat().st_uid,'file_mode':token.stat().st_mode&0o777}
path(str(index)+'.json').write_text(json.dumps(result))
print(json.dumps(result))
"""


def _resident(sandbox_id: str, identity: dict) -> list:
    return _run_json([*_sandbox_exec(sandbox_id), "runuser", "-u", identity["linux_user"], "--", "python3", "-c", """
import json,os,sys
from pathlib import Path
keys=json.loads(sys.argv[1]);found=[]
for p in Path('/proc').iterdir():
 if not p.name.isdecimal():continue
 try:
  if p.stat().st_uid!=os.getuid():continue
  args=p.joinpath('cmdline').read_bytes().split(b'\\0')
  if b'serve' not in args or not any(b'hermes' in arg for arg in args[:2]):continue
  env=dict(x.split(b'=',1) for x in p.joinpath('environ').read_bytes().split(b'\\0') if b'=' in x)
  assert not any(key.encode() in env for key in keys), 'Session context entered the resident process'
  found.append({'pid':int(p.name),'start_ticks':p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]})
 except (FileNotFoundError,ProcessLookupError):pass
profile=(Path(sys.argv[2])/'astrabox-hermes.env').read_text()
assert not any(key+'=' in profile for key in keys), 'Session context entered the shared profile file'
assert len(found)==1, 'native resident process observation must be available'
print(json.dumps(found))
""", json.dumps(_CONTEXT_KEYS), identity["config_dir"]])


@pytest.mark.parametrize("transition", ["reattached", "replaced"])
def test_assistant_tools_keep_each_session_context_after_reattach_and_replacement(
    e2e_client: httpx.Client, live_test_deadline: float, transition: str,
):
    client = e2e_client
    environment = os.environ["ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"]
    model = os.environ["ASTRABOX_E2E_ASSISTANT_MODEL"]
    image = os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE"]
    assert environment.startswith("session-env-")
    principal = data(client.get("/api/v1/user/current"))["user_id"]
    nonce = uuid.uuid4().hex[:8]
    if transition == "replaced":
        # Reuse the retained conversation, including its native history; the
        # replacement itself and every acceptance check remain in this case.
        scene = json.loads(Path(os.environ["ASTRABOX_E2E_ASSISTANT_SCENE_FILE"]).read_text())
        assert scene["owner_user_id"] == principal and scene["environment"] == environment
        aid = scene["assistant_id"]
        sessions = scene["sessions"]
        assert len(sessions) == len(set(sessions)) == 2
        subject = data(client.get(f"/api/v1/assistants/{aid}"))
        assert subject["display_name"].startswith("Session context ") and subject["environment_name"] == environment
    else:
        aid = data(client.post("/api/v1/assistants", json={
            "display_name": f"Session context {nonce}", "environment_name": environment,
            "model_config_override": {"model_name": model},
            "system": "Execute requested terminal commands exactly. Never simulate their output. Reply with the requested completion marker after execution.",
        }))["assistant_id"]
        sessions = []
    release_assistant(aid)
    _wake_assistant(client, aid, timeout=max(0.1, live_test_deadline - time.monotonic()))
    workspace = _assistant_workspace(client, aid, live_test_deadline)
    box = workspace["current_sandbox_id"]
    identity = psql_rows(f"SELECT runtime_identity FROM assistant_workspaces WHERE assistant_id={sql_literal(aid)}")[0]["runtime_identity"]
    resident = _resident(box, identity)
    command = _sandbox_exec(box)
    pod = command[command.index("exec") + 1]
    kubeconfig, namespace = kube_access()
    assert not durable_mounts_of_sandbox(pod=pod, kubeconfig=kubeconfig, namespace=namespace), "this journey must prove core recovery without a workspace volume"
    for index in range(2):
        if transition == "replaced":
            sid = sessions[index]
        else:
            sid = data(client.post(f"/api/v1/assistants/{aid}/conversations", json={}))['session_id']
            sessions.append(sid)
        release_session(sid)
        poll_until_ready(client, sid, expected_image=image if index == 0 else None)
        detail = get_admin_session_detail(client, sid)
        assert detail["user_id"] == principal and detail["assistant_id"] == aid
        assert detail["sandbox_id"] == box
        profile_keys = ("linux_user", "uid", "gid", "home_dir", "workspace_dir", "config_dir")
        assert {key: detail["runtime_identity"][key] for key in profile_keys} == {key: identity[key] for key in profile_keys}
    assert _resident(box, identity) == resident
    previous = {}
    evidence = {"assistant_id": aid, "sessions": sessions, "phases": []}

    def upload_script():
        uploaded = data(client.post(f"/api/v1/sessions/{sessions[0]}/files/upload", data={"path": ""},
                                    files={"files": (f"context-{nonce}.py", _PROBE_SOURCE.encode(), "text/x-python")}))
        return uploaded["entries"][0]["path"]

    script_path = upload_script()

    def round_trip(phase: str, expected_box: str):
        def run(index: int):
            sid = sessions[index]
            output = f"{script_path.removesuffix('.py')}-{phase}-{index}.json"
            marker = f"CONTEXT_{nonce}_{phase}_{index}"
            prompt = f"Run this terminal command, then reply exactly {marker}:\npython3 {shlex.quote(script_path)} {index} {phase}"
            reply = send_and_follow(client, sid, prompt, until="end")
            assert reply.ended and not reply.error and marker in reply.text
            wait_until_settled(client, sid)
            response = download(client, sid, output)
            assert response.status_code == 200, response.text[:300]
            result = response.json()
            assert result["session_id"] == sid
            assert result["url"] == f"https://console.example.test/session/{sid}?from=context&view=full"
            assert result["file_mode"] == 0o600 and result["file_uid"] == result["uid"] == identity["uid"]
            assert result["material"]["session_id"] == sid and result["material"]["principal_id"] == principal
            assert result["material"]["sandbox_id"] == expected_box
            native = _assistant_snapshot(sid, aid, prompt=prompt, deadline=live_test_deadline)
            assert any(row.get("role") == "tool" and sid in str(row.get("content")) and result["url"] in str(row.get("content")) for row in native["messages"])
            visible = json.dumps(history(client, sid))
            assert sid in visible and result["url"] in visible and marker in visible
            if sid in previous:
                old = previous[sid]
                assert native["native_session_id"] == old["native_session_id"]
                assert native["messages"][:len(old["messages"])] == old["messages"]
            previous[sid] = native
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        assert results[0]["session_id"] != results[1]["session_id"]
        evidence["phases"].append({"phase": phase, "box": expected_box, "results": results})
        print(json.dumps({"phase": "assistant-context-stage", "stage": phase, "state": "PASS"}), flush=True)

    round_trip("initial", box)
    if transition == "reattached":
        _restart_server_container(require_service_container(SERVER_CONTAINER_HANDLE))
        _wait_health(str(client.base_url).rstrip("/"))
        target_box = box
    else:
        destroy_box(box)
        replacement = _wake_assistant(client, aid, timeout=max(0.1, live_test_deadline - time.monotonic()))
        target_box = replacement["current_sandbox_id"]
        assert target_box != box
        script_path = upload_script()
    round_trip(transition, target_box)
    assert all(get_admin_session_detail(client, sid)["sandbox_id"] == target_box for sid in sessions)
    after = _resident(target_box, identity)
    if transition == "reattached":
        assert after == resident
    control = "/data/runtime-preparation-session-env-refusals.json"
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    _run_json(["docker", "exec", server, "python", "-c", "from pathlib import Path; import json,sys; Path(sys.argv[1]).write_text(json.dumps([sys.argv[2]])); print('true')", control, aid])
    try:
        refused = data(client.post(f"/api/v1/assistants/{aid}/conversations", json={}))['session_id']
        release_session(refused)
        while time.monotonic() < live_test_deadline:
            detail = get_admin_session_detail(client, refused)
            if detail["state"] == "RECOVERY_REQUIRED":
                break
            time.sleep(0.2)
        assert detail["state"] == "RECOVERY_REQUIRED" and detail["runtime_unavailable"], detail
        assert "environment" in str(detail["last_error"]).lower()
        assert "SECRET_SENTINEL_MUST_NOT_APPEAR" not in json.dumps(detail)
        assert _resident(target_box, identity) == after
        evidence["refused_native_environment"] = {"session_id": refused, "error": detail["last_error"]}
    finally:
        _run_json(["docker", "exec", server, "python", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True); print('true')", control])
    print(json.dumps({"phase": "assistant-session-environment-accepted", **evidence}), flush=True)
