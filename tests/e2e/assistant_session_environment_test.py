"""Concurrent Assistant tools retain their own context across host and box loss."""

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
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
from tests.e2e.test_prepared_runtime_activation import _run_json, _sandbox_exec
from tests.e2e.test_reattach import _restart_server_container

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live, pytest.mark.backend_restart]

_CONTEXT_KEYS = ["RUNTIME_IDENTITY_SESSION_ID", "ASTRABOX_SESSION_URL", "ASTRABOX_CFUSE_AGENT_TOKEN_FILE"]
_PROBE_SOURCE = """
from pathlib import Path
import json,os,pwd,sys,time
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
 'runtime_identity':{'linux_user':pwd.getpwuid(os.getuid()).pw_name,
  'uid':os.getuid(),'gid':os.getgid(),'home_dir':os.environ['HOME'],
  'workspace_dir':str(Path.cwd()),'config_dir':os.environ['HERMES_HOME']},
 'declared_workspace':sys.argv[3],'workspace_realpath':str(Path(sys.argv[3]).resolve()),
 'workspace_same_directory':Path.cwd().samefile(Path(sys.argv[3])),
 'uid':os.getuid(),'file_uid':token.stat().st_uid,'file_mode':token.stat().st_mode&0o777}
path(str(index)+'.json').write_text(json.dumps(result))
print(json.dumps(result))
"""


def _resident(sandbox_id: str, identity: dict) -> list:
    return _run_json([*_sandbox_exec(sandbox_id), "runuser", "-u", identity["linux_user"], "--", "python3", "-c", """
import fcntl,json,os,sys
from pathlib import Path
keys=json.loads(sys.argv[1]);home=Path(sys.argv[2])
record=json.loads((home/'gateway.pid').read_text())
assert record['kind']=='hermes-gateway' and Path(record['hermes_home'])==home
p=Path('/proc')/str(record['pid'])
assert p.stat().st_uid==os.getuid(), 'native gateway runs under a different account'
with (home/'gateway.lock').open() as lock:
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:pass
 else:raise AssertionError('native gateway ownership lock is not held')
env=dict(x.split(b'=',1) for x in p.joinpath('environ').read_bytes().split(b'\\0') if b'=' in x)
assert not any(key.encode() in env for key in keys), 'Session context entered the resident process'
found=[{'pid':record['pid'],'start_ticks':p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]}]
profile=(Path(sys.argv[2])/'astrabox-hermes.env').read_text()
assert not any(key+'=' in profile for key in keys), 'Session context entered the shared profile file'
assert len(found)==1, 'native resident process observation must be available'
print(json.dumps(found))
""", json.dumps(_CONTEXT_KEYS), identity["config_dir"]])


def _context_snapshots(assistant_id: str, principal: str, prompts: dict[str, str], deadline: float) -> dict:
    """Read both conversations from one published native SQLite archive, without booting another app."""
    selected = ",".join(sql_literal(sid) for sid in prompts)
    query = f"""
SELECT to_jsonb(h) AS head, w.current_sandbox_id,
 (SELECT jsonb_agg(to_jsonb(q)) FROM (
   SELECT session_id,user_id,assistant_id,workspace_kind,engine_kind,engine_session_key
   FROM sessions WHERE session_id IN ({selected})
 ) q) AS sessions,
 (SELECT jsonb_agg(jsonb_build_object('index',c.chunk_index,'data',encode(c.data,'base64')) ORDER BY c.chunk_index)
  FROM runtime_state_chunks c WHERE c.archive=h.archive AND c.owner_key=h.owner_key
  AND c.snapshot_id=h.snapshot_id) AS chunks
FROM assistant_workspaces w LEFT JOIN runtime_state_heads h
 ON h.archive='native_session_store' AND h.subject_kind='assistant'
 AND h.subject_id=w.assistant_id AND h.user_id=w.created_by_user_id AND h.engine_kind=w.engine_kind
WHERE w.assistant_id={sql_literal(assistant_id)}
"""
    while time.monotonic() < deadline:
        rows = psql_rows(query)
        assert len(rows) == 1
        row = rows[0]
        head = row["head"]
        if head is None:
            time.sleep(0.5)
            continue
        assert (head["user_id"], head["subject_kind"], head["subject_id"], head["engine_kind"]) == (
            principal, "assistant", assistant_id, "assistant",
        )
        chunks = row["chunks"] or []
        assert [c["index"] for c in chunks] == list(range(head["chunk_count"]))
        pieces = [base64.b64decode("".join(c["data"].split()), validate=True) for c in chunks]
        assert [len(b) for b in pieces] == [min(256 * 1024, head["size"] - i * 256 * 1024) for i in range(len(pieces))]
        payload = b"".join(pieces)
        assert len(payload) == head["size"] and hashlib.sha256(payload).hexdigest() == head["sha256"]
        assert payload.startswith(b"SQLite format 3\x00")
        assert {entry["session_id"] for entry in row["sessions"]} == set(prompts)
        result = {}
        with tempfile.TemporaryDirectory(prefix="astrabox-context-native-") as temporary:
            path = Path(temporary) / "state.db"
            path.write_bytes(payload)
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                assert [r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"]
                for entry in row["sessions"]:
                    sid = entry["session_id"]
                    assert entry["user_id"] == principal and entry["assistant_id"] == assistant_id
                    assert (entry["workspace_kind"], entry["assistant_id"], entry["user_id"], entry["engine_kind"]) == (
                        "assistant", assistant_id, principal, "assistant",
                    )
                    native_id = entry["engine_session_key"]
                    assert native_id and db.execute("SELECT id FROM sessions WHERE id=?", (native_id,)).fetchone()
                    messages = [{key: {"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value
                                 for key, value in dict(record).items()} for record in db.execute(
                                     "SELECT * FROM messages WHERE session_id=? ORDER BY id", (native_id,),
                                 )]
                    result[sid] = {"native_session_id": native_id, "messages": messages,
                                   "snapshot_id": head["snapshot_id"], "sha256": head["sha256"], "size": head["size"],
                                   "current_sandbox_id": row["current_sandbox_id"]}
        complete = True
        for sid, native in result.items():
            users = [i for i, message in enumerate(native["messages"])
                     if message.get("role") == "user" and message.get("content") == prompts[sid]]
            assert len(users) <= 1, "the same explicit input was recorded twice in native history"
            if not users or not any(m.get("role") == "assistant" and m.get("content")
                                    for m in native["messages"][users[0] + 1:]):
                complete = False
        if complete:
            return result
        time.sleep(0.5)
    pytest.fail("both completed native turns did not reach one published platform archive before the deadline")


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
        scene = json.loads(Path(os.environ["ASTRABOX_E2E_ASSISTANT_SCENE_FILE"]).read_text())
        assert scene["owner_user_id"] == principal and scene["environment"] == environment
        aid, sessions = scene["assistant_id"], scene["sessions"]
        assert len(sessions) == len(set(sessions)) == 2
        subject = data(client.get(f"/api/v1/assistants/{aid}"))
        assert subject["display_name"].startswith("Session context ") and subject["runtime_profile_name"] == environment
    else:
        aid = data(client.post("/api/v1/assistants", json={
            "display_name": f"Session context {nonce}", "runtime_profile_name": environment,
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
    assert _resident(box, identity) == resident
    previous = {}
    evidence = {"assistant_id": aid, "sessions": sessions, "resident_initial": resident, "phases": []}

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
            prompt = f"Run this terminal command, then reply exactly {marker}:\npython3 {shlex.quote(script_path)} {index} {phase} {shlex.quote(identity['workspace_dir'])}"
            reply = send_and_follow(client, sid, prompt, until="end")
            assert reply.ended and not reply.error and marker in reply.text
            wait_until_settled(client, sid)
            response = download(client, sid, output)
            assert response.status_code == 200, response.text[:300]
            result = response.json()
            assert result["session_id"] == sid
            profile_keys = ("linux_user", "uid", "gid", "home_dir", "config_dir")
            assert {key: result["runtime_identity"][key] for key in profile_keys} == {key: identity[key] for key in profile_keys}
            assert result["declared_workspace"] == identity["workspace_dir"]
            assert result["runtime_identity"]["workspace_dir"] == result["workspace_realpath"]
            assert result["workspace_same_directory"] is True
            assert result["url"] == f"https://console.example.test/session/{sid}?from=context&view=full"
            assert result["file_mode"] == 0o600 and result["file_uid"] == result["uid"] == identity["uid"]
            assert result["material"]["session_id"] == sid and result["material"]["principal_id"] == principal
            assert result["material"]["sandbox_id"] == expected_box
            visible = json.dumps(history(client, sid))
            assert sid in visible and result["url"] in visible and marker in visible
            return prompt, result

        with ThreadPoolExecutor(max_workers=2) as pool:
            pairs = list(pool.map(run, range(2)))
        results = [result for _prompt, result in pairs]
        native_snapshots = _context_snapshots(aid, principal, dict(zip(sessions, [prompt for prompt, _ in pairs])), live_test_deadline)
        for sid, result in zip(sessions, results):
            native = native_snapshots[sid]
            assert native["current_sandbox_id"] == expected_box
            assert any(row.get("role") == "tool" and sid in str(row.get("content")) and result["url"] in str(row.get("content")) for row in native["messages"])
            if sid in previous:
                old = previous[sid]
                assert native["native_session_id"] == old["native_session_id"]
                assert native["messages"][:len(old["messages"])] == old["messages"]
            previous[sid] = native
        assert results[0]["session_id"] != results[1]["session_id"]
        evidence["phases"].append({"phase": phase, "box": expected_box, "results": results, "native_snapshots": native_snapshots})
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
    evidence["resident_after"] = after
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
