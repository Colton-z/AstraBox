"""Native startup notices stay local while both channel delivery modes continue."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile, data, get_session, history, message_text, permission_mode,
    poll_until_ready, psql, psql_rows, release_agent, release_session, run_terminal,
    sql_literal, transcript_entries, wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def test_startup_notices_stay_in_history_while_channel_replies_continue(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Real startup/resume hooks never become outbox rows or streamed cards."""
    client = e2e_client
    nonce = uuid.uuid4().hex
    welcome, launched, completed = (f"{kind}_{nonce}" for kind in ("NOTICE", "LAUNCHED", "COMPLETED"))
    received: list[tuple[str, dict]] = []

    class Recipient(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    recipient = ThreadingHTTPServer(("0.0.0.0", 0), Recipient)
    threading.Thread(target=recipient.serve_forever, daemon=True).start()
    host = urlparse(os.environ["ASTRABOX_E2E_BASE_URL"]).hostname
    assert host and host not in {"localhost", "127.0.0.1"}
    callback = f"http://{host}:{recipient.server_port}"
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code"
    program = (
        "import json,sys; from pathlib import Path; d=json.load(sys.stdin); "
        f"m={welcome!r}+'_'+d['source']; "
        f"p=Path('/tmp/notice-{nonce}.jsonl'); "
        "p.open('a').write(json.dumps({'source':d['source'],'message':m})+'\\n'); "
        "print(json.dumps({'systemMessage':m}))"
    )
    agent = data(client.post("/api/v1/agents", json={
        "name": f"Channel notice {nonce}", "model": profile["model"],
        "runtime_profile_name": profile["runtime_profile_name"], "prepared_capacity": 0,
        "engine_options": {"sdk_options": {"include_hook_events": True, "settings": {
            "hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": [{
                "type": "command", "timeout": 10, "command": "python3 -c " + shlex.quote(program),
            }]}]},
        }}},
    }))
    aid = agent["agent_id"]
    release_agent(aid)
    deployments = [data(client.post(f"/api/v1/admin/agents/{aid}/deployments", json={
        "scene": "channel:" + name, "prompt_prefix": "",
    })) for name in ("generic_json", "installed_stream_probe")]
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    sid = ""
    passed = False

    def query(table: str, columns: str = "*") -> list[dict]:
        return psql_rows(f"SELECT {columns} FROM {table} WHERE session_id={sql_literal(sid)}")

    def wait(predicate, description):
        while time.monotonic() < live_test_deadline:
            if result := predicate():
                return result
            time.sleep(0.2)
        pytest.fail(description)

    def trigger(prompt: str) -> str:
        binding = deployments[0]
        return data(client.post(f"/api/v1/deployments/{binding['deployment_id']}/trigger", headers={
            "x-channel-secret": binding["secret"],
        }, json={"text": prompt, "message_id": str(uuid.uuid4()), "conversation_id": nonce,
                 "reply": {"callback_url": callback + "/final"}}))["session_id"]

    def projected():
        last = psql_rows(f"SELECT coalesce(max(seq),0) AS seq FROM session_output "
                         f"WHERE session_id={sql_literal(sid)} AND part->>'type'='finish'")[0]["seq"]
        subs = query("channel_output_subscriptions")
        return len(subs) == 2 and all(row["after_seq"] >= last for row in subs)

    def final_texts():
        return [body["text"] for path, body in received if path == "/final"]

    def stream_cards():
        return {body["message_id"]: body for path, body in received if path == "/stream"}

    def terminal(command):
        stdout, stderr, code, _events = run_terminal(client, sid, command)
        assert code == 0, stderr
        return stdout

    try:
        sid = trigger("Explain in one short sentence what a calendar is. Do not use tools.")
        release_session(sid)
        # A second destination follows the same real output; the first binding
        # exercises the normal HTTP ingress and subscription path.
        subscribe = """
import asyncio,json,sys
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.persistence.repository.channel_repository import ChannelRepository
asyncio.run(ChannelRepository().subscribe_to_output(
    session_id=sys.argv[1],deployment_id=sys.argv[2],channel_name='installed_stream_probe',
    reply_context={'callback_url':sys.argv[3]},conversation_key=sys.argv[4],after_seq=0,streaming=True))
"""
        subprocess.run(["docker", "exec", server, "python", "-c", subscribe, sid,
                        deployments[1]["deployment_id"], callback + "/stream", nonce],
                       check=True, capture_output=True, timeout=30)
        poll_until_ready(client, sid)
        data(client.post(f"/api/v1/sessions/{sid}/permission-mode", json={
            "permission_mode": permission_mode("unattended"),
        }))
        wait(lambda: len(final_texts()) == len(stream_cards()) == 1 and projected(), "first reply was not delivered")
        wait_until_settled(client, sid)
        original_box = get_session(client, sid)["sandbox_id"]
        native_receipt = terminal(f"cat /tmp/notice-{nonce}.jsonl")
        assert welcome + "_startup" in native_receipt
        notices = [row for row in history(client, sid) if welcome in message_text(row)]
        assert len(notices) == 1 and message_text(notices[0]) == welcome + "_startup", notices
        assert not any(welcome in text for text in final_texts())
        assert not any(welcome in (body.get("text") or "") for _, body in received)
        assert len(query("channel_outbox")) == 2

        reclaimed = data(client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
        assert reclaimed["status"] == "sandbox-reclaimed", reclaimed
        release_file = f"notice-release-{nonce}"
        command = "\n".join([
            "python3 - <<'PY'", "from pathlib import Path", "import time",
            f"p=Path('/tmp/{release_file}')", "deadline=time.monotonic()+90",
            "while not p.exists():", "    if time.monotonic()>deadline: raise TimeoutError()",
            "    time.sleep(0.2)", f"print({completed!r})", "PY",
        ])
        prompt = (
            "Launch exactly one Agent with run_in_background=true. The child must use Bash to run exactly: "
            f"{command}\nThe child must report {completed} verbatim after the command completes. "
            f"Immediately reply only {launched} and end your turn without waiting or running Bash in the parent. "
            f"When the child's completion notice arrives, send a new reply containing {completed}. "
            "Do not mention the completion token before that notice."
        )
        assert trigger(prompt) == sid
        wait(lambda: len(final_texts()) == len(stream_cards()) == 2 and projected(), "resumed reply was not delivered")
        inputs = [row for row in query("session_responses") if row["origin"] == "input"]
        assert len(inputs) == 2 and all(row["status"] == "ended" and row["outcome"] == "completed" for row in inputs)
        assert launched in final_texts()[1] and completed not in final_texts()[1]
        assert get_session(client, sid)["sandbox_id"] != original_box
        native_receipt = terminal(f"cat /tmp/notice-{nonce}.jsonl")
        assert welcome + "_resume" in native_receipt, native_receipt
        notices = [row for row in history(client, sid) if welcome in message_text(row)]
        assert sorted(message_text(row) for row in notices) == [welcome + "_resume", welcome + "_startup"], notices
        assert len({row["id"] for row in notices}) == 2
        assert not any(welcome in (body.get("text") or "") for _, body in received)
        children = wait(lambda: data(client.get(f"/api/v1/sessions/{sid}/child-runs")).get("child_runs"),
                        "no real child was launched")
        assert len(children) == 1 and children[0]["active"], children
        terminal(f"touch /tmp/{release_file}")
        wait(lambda: len(final_texts()) == len(stream_cards()) == 3 and projected(), "native background reply was not delivered")
        wait_until_settled(client, sid)
        assert completed in final_texts()[-1]
        native = transcript_entries(sid)
        blocks = [(row["subpath"], block) for row in native
                  for block in (row["entry"].get("message") or {}).get("content", []) if isinstance(block, dict)]
        commands = [block for scope, block in blocks if scope and block.get("type") == "tool_use"
                    and (block.get("input") or {}).get("command") == command]
        assert len(commands) == 1, commands
        assert any(scope and block.get("type") == "tool_result"
                   and block.get("tool_use_id") == commands[0]["id"]
                   and not block.get("is_error") and completed in str(block.get("content"))
                   for scope, block in blocks)
        assert any(not scope and block.get("type") == "text" and completed in block["text"]
                   for scope, block in blocks)
        responses = query("session_responses")
        notice_ids = {row["id"] for row in notices}
        assert {row["response_id"] for row in responses if row["native_reason"] == "session_start"} == notice_ids
        outbox = query("channel_outbox")
        assert len(outbox) == 6 and all(row["state"] == "DELIVERED" for row in outbox), outbox
        assert not any(row["response_id"] in notice_ids for row in outbox)
        assert any(row["origin"] == "engine" and row["response_id"] not in notice_ids
                   and row["outcome"] == "completed" for row in responses)
        expected = ["\n\n".join(part["text"] for part in row["parts"] if part["type"] == "text").strip()
                    for row in history(client, sid)
                    if row["role"] == "assistant" and row["id"] not in notice_ids]
        assert final_texts() == expected
        assert [body["text"] for body in stream_cards().values()] == expected
        assert all(body["type"] == "settled" for body in stream_cards().values())
        # The reconciler replays the same log after the cursor is lost.
        assert len(psql(f"UPDATE channel_output_subscriptions SET after_seq=0 "
                        f"WHERE session_id={sql_literal(sid)} RETURNING deployment_id")) == 2
        wait(projected, "subscription cursors did not advance across notice replay")
        assert query("channel_outbox") == outbox
        assert final_texts() == expected and len(stream_cards()) == 3
        print(json.dumps({"notice_ids": sorted(notice_ids), "normal_inputs": 2,
                          "final_replies": len(final_texts()), "stream_cards": len(stream_cards()),
                          "outbox_rows": len(outbox), "native_background_delivered": True,
                          "subscription_replay_preserved_outbox": True}))
        passed = True
    finally:
        if passed:
            for binding in deployments:
                data(client.delete(f"/api/v1/admin/agents/{aid}/deployments/{binding['deployment_id']}"))
        recipient.shutdown()
        recipient.server_close()
