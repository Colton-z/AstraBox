"""An Assistant input the platform hands to Hermes again after its reader was replaced runs once.

The reader is held after Hermes received the input and before the platform
recorded that Hermes holds it, so the platform keeps custody of an input whose
first delivery's outcome it never saw. Hermes' gateway then restarts while the
input's command runs; shutting down, it interrupts that turn and settles it,
never to run it again. The restarted gateway is a new instance, so the
replacement reader's cursor is expired and the platform hands the input over
again. The gateway admits a request id once: the second delivery is answered
with the first admission, the turn the restart cut off is reported stopped,
and the conversation answers its next message.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    data,
    download,
    list_files,
    poll_until_ready,
    psql,
    recorded_input,
    recorded_response,
    release_assistant,
    release_session,
    send_and_follow,
    send_input,
    sql_literal,
    wait_for_file,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.runtime_preparation_test import _server_log
from tests.e2e.test_conversation_end import _assistant_runtime, _wake_assistant
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live]

#: The admission request id the Hermes adapter submits an input under
#: (``INPUT_REQUEST_PREFIX`` in ``engine/hermes_runtime_client.py``).
_REQUEST_PREFIX = "astrabox-input:"
_RECEIPT_LOG = re.compile(r"runtime diagnostic session=(\S+) type=hermes\.prompt\.submit raw=(\{.*\})")


def _admission_hold(sid: str, action: str) -> dict:
    """Arm, read, release or clear one reader hold before the input's admission is recorded."""
    program = """
import json,pathlib,sys
from astrabox.testing.e2e_faults import _fault_base_path
sid,action=sys.argv[1:]
base=_fault_base_path()
assert base, 'the server does not arm E2E faults'
p=pathlib.Path(base+'.d')/('hermes-redelivery-'+sid+'.json')
if action=='clear': p.unlink(missing_ok=True)
elif action in ('arm','release'):
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'hold_reader_boundary':1},'match':{'session_id':sid,'phase':'admission'},'release':False,'consumed':[]} if action=='arm' else {**json.loads(p.read_text()),'release':True}
 t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.chmod(0o644);t.replace(p)
print(p.read_text() if p.exists() else '{}')
"""
    return json.loads(subprocess.check_output([
        "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
        "python", "-c", program, sid, action,
    ], text=True, timeout=10))


def _in_box(box: str, script: str) -> str:
    result = subprocess.run(
        [*_sandbox_exec(box), "bash", "-c", script], capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"box command failed: {result.stderr[-800:]}"
    return result.stdout


def _admissions(box: str, config_dir: str, request_id: str) -> list[dict]:
    """The gateway's own ledger rows for one admission request id, read from its state.db."""
    program = (
        "import json,sqlite3,sys\n"
        "c=sqlite3.connect('file:'+sys.argv[1]+'/state.db?mode=ro',uri=True)\n"
        "c.row_factory=sqlite3.Row\n"
        "print(json.dumps([dict(r) for r in c.execute("
        "'SELECT admission_id,status,outcome FROM session_admissions WHERE request_id=?',(sys.argv[2],))]))\n"
    )
    return json.loads(_in_box(box, f"python3 -c {shlex.quote(program)} {shlex.quote(config_dir)} "
                                   f"{shlex.quote(request_id)}"))


def _user_turns(box: str, config_dir: str, text: str) -> int:
    """How many user messages in the gateway's own transcript (its state.db) carry ``text``."""
    program = (
        "import sqlite3,sys\n"
        "c=sqlite3.connect('file:'+sys.argv[1]+'/state.db?mode=ro',uri=True)\n"
        "print(c.execute(\"SELECT count(*) FROM messages WHERE role='user' AND instr(content,?)>0\",(sys.argv[2],)).fetchone()[0])\n"
    )
    return int(_in_box(box, f"python3 -c {shlex.quote(program)} {shlex.quote(config_dir)} {shlex.quote(text)}"))


def _receipts(since: float, sid: str, input_id: str) -> list[dict]:
    """The Hermes admission receipts the platform's readers logged for one input since ``since``."""
    found = []
    for line in _server_log(require_service_container(SERVER_CONTAINER_HANDLE), since).splitlines():
        match = _RECEIPT_LOG.search(line)
        if match and match.group(1) == sid:
            raw = json.loads(match.group(2))
            if raw.get("input_id") == input_id:
                found.append(raw["receipt"])
    return found


def _wait(read, done, description: str, deadline: float):
    value = None
    while time.monotonic() < deadline:
        value = read()
        if done(value):
            return value
        time.sleep(0.5)
    raise AssertionError(f"{description}: {value!r}")


def test_an_input_handed_over_again_after_its_reader_was_replaced_runs_once(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    assistant_id = sid = ""
    try:
        environment, image, _digest, model, _kubeconfig, _namespace = _assistant_runtime(e2e_client)
        assistant_id = str(data(e2e_client.post("/api/v1/assistants", json={
            "display_name": f"__e2e_redelivery_{uuid.uuid4().hex[:8]}",
            "runtime_profile_name": environment,
            "model_config_override": {"model_name": model},
        })).get("assistant_id") or "")
        assert assistant_id
        workspace = _wake_assistant(e2e_client, assistant_id, timeout=max(0.1, live_test_deadline - time.monotonic()))
        box = str(workspace["current_sandbox_id"])
        sid = str(data(e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={}))["session_id"])
        poll_until_ready(e2e_client, sid, expected_image=image)
        root = str(list_files(e2e_client, sid).get("root_path") or "")
        assert root == "/workspace", root
        config_dir = psql(
            "SELECT runtime_identity->>'config_dir' FROM assistant_workspaces "
            f"WHERE assistant_id={sql_literal(assistant_id)}"
        )[0]
        marker = uuid.uuid4().hex
        runs, release = f"{root}/{marker}.runs", f"{root}/{marker}.release"
        command = (
            f"printf 'run\\n' >> {shlex.quote(runs)}; "
            f"while [ ! -e {shlex.quote(release)} ]; do sleep 0.1; done"
        )
        prompt = (
            "Use the terminal tool to run exactly this one command in the foreground and wait "
            f"for it to finish. Do not create the release file or run any other command: {command}"
        )

        _admission_hold(sid, "arm")
        input_id = str(send_input(e2e_client, sid, prompt)["input_id"])
        # Hermes holds the input and runs its command; the platform still has custody of it.
        _wait(lambda: _admission_hold(sid, "read").get("consumed") or [],
              lambda consumed: any(item.get("item_id") == input_id for item in consumed),
              "the reader never reached the input's admission", live_test_deadline)
        assert wait_for_file(e2e_client, sid, runs, timeout=60.0) == b"run\n"
        first = recorded_input(sid, input_id)
        assert first["state"] == "delivered" and first["delivered_at"], first

        # A new gateway instance: the replacement reader cannot resume its predecessor's cursor.
        _in_box(box, "supervisorctl restart astrabox-hermes")
        released = time.time()
        _admission_hold(sid, "release")

        again = _wait(lambda: recorded_input(sid, input_id),
                      lambda row: row["delivered_at"] not in {None, first["delivered_at"]} and row["response_id"],
                      "the platform never handed the input to Hermes again", live_test_deadline)
        lost = _wait(lambda: recorded_response(sid, again["response_id"]), lambda row: row["status"] == "ended",
                     "the turn the restart cut off never ended", live_test_deadline)
        assert (lost["outcome"], lost["native_reason"], lost["input_ids"]) == ("cancelled", "interrupted", [input_id]), lost
        assert "will not run it again" in json.dumps(lost["error"]), lost

        _in_box(box, f"touch {shlex.quote(release)}")
        next_prompt = f"Reply with exactly {marker}_NEXT and nothing else. Do not use any tool."
        following = send_and_follow(e2e_client, sid, next_prompt, until="end")
        assert following.error is None and f"{marker}_NEXT" in following.text, following.error or following.text
        wait_until_settled(e2e_client, sid)

        # The command ran once, Hermes' ledger holds one admission for the input,
        # settled interrupted, and answered the second delivery with it, and
        # Hermes' own transcript holds the input's message once.
        assert download(e2e_client, sid, runs).content == b"run\n"
        rows = _admissions(box, config_dir, _REQUEST_PREFIX + input_id)
        assert len(rows) == 1 and (rows[0]["status"], rows[0]["outcome"]) == ("terminal", "interrupted"), rows
        receipts = _receipts(released, sid, input_id)
        assert receipts and {receipt["admission_id"] for receipt in receipts} == {rows[0]["admission_id"]}, receipts
        assert lost["response_id"] == f"hermes:{rows[0]['admission_id']}"
        responses = psql(
            "SELECT r.response_id FROM session_responses r JOIN session_response_inputs i "
            "USING(session_id,response_id) "
            f"WHERE r.session_id={sql_literal(sid)} AND i.input_id={sql_literal(input_id)}"
        )
        assert responses == [lost["response_id"]], responses
        assert _user_turns(box, config_dir, prompt) == 1
        print(json.dumps({"phase": "hermes-redelivery-accepted", "session_id": sid, "input_id": input_id,
                          "first_delivery": first, "second_delivery": again, "lost": lost,
                          "admissions": rows, "receipts": receipts}, default=str), flush=True)
    finally:
        if sid:
            _admission_hold(sid, "clear")
        release_session(sid)
        release_assistant(assistant_id)
