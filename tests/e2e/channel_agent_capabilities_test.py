"""A messaging-channel conversation is the Agent's conversation, run as its execution account.

The trigger runs as the Agent's creator, so the conversation is given the
tools the creator's console conversations of the Agent are given. Those are
the tools a prepared runtime of the Agent already holds, so the conversation
claims one, both when it starts and when it continues after its box was
reclaimed. The Agent keeps two prepared boxes, so the continuation finds one
waiting instead of waiting for a refill. The reply is delivered to a recipient inside the server's
container, so each step a group message waits on is timed from the same clock.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import datetime

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile,
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    get_session,
    output_position,
    psql_rows,
    release_agent,
    release_session,
    sql_literal,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_prepared_runtime_activation import (
    _prepared_identity,
    _run_json,
    _sandbox_exec,
    _supplier_processes,
    _wait_prepared_count,
)

pytestmark = pytest.mark.e2e

# Started detached in the server container, where the channel provider posts
# its replies; it records each body with the time it arrived.
_RECIPIENT = """
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
log = sys.argv[1]
class Recipient(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with open(log, 'a') as out:
            out.write(json.dumps({'at': time.time(), 'body': body}) + '\\n')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self, *args):
        pass
server = HTTPServer(('127.0.0.1', 0), Recipient)
with open(log + '.port', 'w') as out:
    json.dump({'port': server.server_port, 'pid': os.getpid()}, out)
server.serve_forever()
"""

_READ = """
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
print(json.dumps([json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []))
"""

_STOP = """
import json, os, signal, sys
from pathlib import Path
port = Path(sys.argv[1] + '.port')
if port.exists():
    os.kill(json.loads(port.read_text())['pid'], signal.SIGTERM)
    port.unlink()
Path(sys.argv[1]).unlink(missing_ok=True)
"""

def _assert_prepared_before(client: httpx.Client, sandbox_id: str, sent_at: datetime) -> None:
    """The box existed before the message: the conversation claimed it, not a cold create."""
    box = data(client.get(f"/api/v1/admin/sandboxes/{sandbox_id}"))
    created_at = datetime.fromisoformat(str(box["created_at"]).replace("Z", "+00:00"))
    assert created_at < sent_at, f"sandbox {sandbox_id} was created at {created_at}, after {sent_at}"


def _assert_supplier_started_before(execute: list[str], processes: list, sent_at: datetime) -> None:
    """Every native supplier process was already running when the message was sent."""
    clock = _run_json([*execute, "python3", "-c",
                       "import json, os; print(json.dumps([int(open('/proc/stat').read().split('btime ')[1]"
                       ".split()[0]), os.sysconf('SC_CLK_TCK')]))"])
    assert isinstance(clock, list)
    boot, hertz = clock
    started = [boot + int(process["start_ticks"]) / hertz for process in processes]
    assert max(started) < sent_at.timestamp(), (processes, started, sent_at)


_ENGINE_EVENT = (
    "part->>'type' NOT IN ('start', 'start-step', 'finish-step', 'finish') "
    "AND part->>'type' NOT LIKE 'data-%'"
)


def test_a_channel_conversation_uses_the_agents_tools_and_claims_its_prepared_runtime(
    e2e_client: httpx.Client, live_test_deadline: float, request: pytest.FixtureRequest,
) -> None:
    """Publish an artifact from a group message, then continue after the box is reclaimed."""
    client = e2e_client
    profile = current_profile()
    engine = profile["engine_kind"]
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    settings = _run_json([
        "docker", "exec", server, "python", "-c", "import json; from astrabox.common.utils.settings import "
        "load_astrabox_settings; print(json.dumps({'volume': "
        "load_astrabox_settings().sandbox_workspace_volume or ''}))",
    ])
    assert settings == {"volume": ""}, "This case continues the conversation without a workspace volume"
    nonce = uuid.uuid4().hex
    title, label = f"Channel artifact {nonce[:12]}", f"LABEL_{nonce[12:24].upper()}"
    log = f"/tmp/astrabox-e2e-channel-{nonce}.jsonl"
    canonical = data(client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(client.post("/api/v1/agents", json={
        "name": f"Channel capabilities {nonce[:8]}",
        "model": profile["model"],
        "runtime_profile_name": environment_with_tenancy(client, "conversation"),
        "prepared_capacity": 2,
        "engine_options": canonical.get("engine_options") or {},
    }))
    agent_id = agent["agent_id"]
    release_agent(agent_id)
    assert agent["artifacts_enabled"] is True, agent
    binding = data(client.post(f"/api/v1/admin/agents/{agent_id}/deployments", json={
        "scene": "channel:generic_json", "prompt_prefix": "",
    }))
    subprocess.run(["docker", "exec", "-d", server, "python", "-c", _RECIPIENT, log],
                   check=True, capture_output=True, timeout=30)
    try:
        while not (port := _run_json(["docker", "exec", server, "python", "-c", _READ, log + ".port"])):
            assert time.monotonic() < live_test_deadline, "the reply recipient did not start"
            time.sleep(0.1)
        callback = f"http://127.0.0.1:{port[0]['port']}/reply"

        def trigger(text: str) -> str:
            return data(client.post(f"/api/v1/deployments/{binding['deployment_id']}/trigger", headers={
                "x-channel-secret": binding["secret"],
            }, json={"text": text, "message_id": str(uuid.uuid4()), "conversation_id": nonce,
                     "reply": {"callback_url": callback}}))["session_id"]

        def delivered(count: int) -> list[dict]:
            while time.monotonic() < live_test_deadline:
                rows = _run_json(["docker", "exec", server, "python", "-c", _READ, log])
                assert isinstance(rows, list)
                if len(rows) >= count:
                    return rows
                time.sleep(0.2)
            pytest.fail(f"the channel delivered fewer than {count} replies")

        _wait_prepared_count(client, agent_id, 2, live_test_deadline)

        first_sent_at = datetime.now().astimezone()
        sid = trigger(
            f"Our hypothetical research project is labelled {label}. Publish a one-page HTML "
            f"artifact with the artifacts tool, titled exactly \"{title}\", whose body explains "
            "revenue growth in one sentence. Do not put the project label in the artifact. "
            "Then reply with the artifact's title."
        )
        release_session(sid)
        first = delivered(1)[0]["body"]
        wait_until_settled(client, sid)
        assert get_session(client, sid)["source_type"] == "channel"
        session = get_admin_session_detail(client, sid)
        first_box = session["sandbox_id"]
        _assert_prepared_before(client, first_box, first_sent_at)
        execute = _sandbox_exec(first_box)
        receipt = _prepared_identity(agent_id, "conversation", execute)
        assert receipt["sandbox_id"] == first_box, receipt
        for key in ("home_dir", "linux_user", "isolated_session_id", "workspace_dir"):
            assert (session["runtime_identity"].get(key) or "") == (receipt["runtime_identity"].get(key) or ""), key
        _assert_supplier_started_before(
            execute, _supplier_processes(execute, receipt["runtime_identity"], engine), first_sent_at,
        )
        published = data(client.get("/api/v1/artifacts", params={"session_id": sid}))["items"]
        assert [item["title"] for item in published] == [title], (published, first)
        assert published[0]["origin"]["agent_id"] == agent_id, published[0]
        native_session = session["engine_session_key"]
        assert native_session, "A native conversation must exist before the box is reclaimed"

        reclaimed = data(client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
        assert reclaimed["sandbox_id"] == first_box and reclaimed["killed"] is True, reclaimed
        position = output_position(client, sid)
        second_sent_at = datetime.now().astimezone()
        sent_at = second_sent_at.timestamp()
        assert trigger(
            "What was the project label of our research project? Answer from this "
            "conversation, without tools."
        ) == sid
        # The restored runtime echoes the message it took (data-input-consumed),
        # then streams its first event; both are durable output rows.
        seen: dict[str, float] = {}
        while len(seen) < 2:
            assert time.monotonic() < live_test_deadline, "the continued conversation did not answer"
            for row in psql_rows(
                "SELECT CASE WHEN part->>'type'='data-input-consumed' THEN 'taken' ELSE 'event' END AS kind, "
                "extract(epoch FROM created_at)::float8 AS at FROM session_output "
                f"WHERE session_id={sql_literal(sid)} AND seq>{position} "
                f"AND (part->>'type'='data-input-consumed' OR ({_ENGINE_EVENT})) ORDER BY seq"
            ):
                seen.setdefault(row["kind"], row["at"])
            time.sleep(0.2)
        second = delivered(2)[1]
        assert label in second["body"]["text"], second
        wait_until_settled(client, sid)
        resumed = get_admin_session_detail(client, sid)
        resumed_box = resumed["sandbox_id"]
        assert resumed_box != first_box
        _assert_prepared_before(client, resumed_box, second_sent_at)
        waiting = _prepared_identity(agent_id, "conversation", _sandbox_exec(resumed_box))
        assert waiting["sandbox_id"] == resumed_box, waiting
        for key in ("home_dir", "linux_user", "isolated_session_id", "workspace_dir"):
            assert (resumed["runtime_identity"].get(key) or "") == (waiting["runtime_identity"].get(key) or ""), key
        assert resumed["engine_session_key"] == native_session
        evidence = json.dumps({
            "engine": engine, "session_id": sid, "artifact_id": published[0]["artifact_id"],
            "first_sandbox": first_box, "resumed_sandbox": resumed_box,
            "native_session": native_session,
            "continued_seconds": {
                "to_runtime_took_input": round(seen["taken"] - sent_at, 3),
                "to_first_engine_event": round(seen["event"] - sent_at, 3),
                "to_channel_delivery": round(second["at"] - sent_at, 3),
            },
        })
        request.node.user_properties.append(("channel_capabilities", evidence))
        print(evidence, flush=True)
    finally:
        subprocess.run(["docker", "exec", server, "python", "-c", _STOP, log],
                       capture_output=True, timeout=30)
