"""An Assistant sandbox whose bridge port is served by another program says so, and resumes once recovered.

The bridge answers its readiness path 200 or 503. A sandbox image from before
it serves that port with another program, which answers 404, and no amount of
waiting changes which program serves a port. So the reader records the
mismatch as soon as the port answers, instead of spending the startup budget
and retrying it unseen. The conversation then reads recovery required with the
reason, and the message that was waiting stays with the platform, undelivered,
under its own id. With the bridge back, recovering the conversation delivers
that message once and it is answered.
"""

from __future__ import annotations

import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    data,
    get_session,
    poll_until_ready,
    psql,
    recorded_input,
    recorded_response,
    release_assistant,
    release_session,
    send_and_follow,
    send_input,
    sql_literal,
    wait_until_settled,
)
from tests.e2e.test_conversation_end import _assistant_runtime, _wake_assistant
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live]

#: The port and readiness path the Hermes bridge serves (``HERMES_BACKEND_PORT``
#: and ``HERMES_BRIDGE_HEALTH_PATH`` in ``engine/hermes.py``).
_BRIDGE_PORT = 9118
_HEALTH = f"http://127.0.0.1:{_BRIDGE_PORT}/healthz"
#: Well inside the 300 s the probe allows a bridge that has not started.
_RECORDED_WITHIN_S = 60.0


def _in_box(box: str, script: str) -> str:
    result = subprocess.run(
        [*_sandbox_exec(box), "bash", "-c", script], capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"box command failed: {result.stderr[-800:]}"
    return result.stdout


def _health(box: str) -> str:
    return _in_box(box, f"curl -s -o /dev/null -m 3 -w '%{{http_code}}' {_HEALTH} || true").strip()


def _pattern(marker: str) -> str:
    """A ``pkill -f`` pattern for ``marker`` that the command line running it does not match."""
    return shlex.quote(f"[{marker[0]}]{marker[1:]}")


def _wait(read, done, description: str, deadline: float):
    value = None
    while time.monotonic() < deadline:
        value = read()
        if done(value):
            return value
        time.sleep(0.5)
    raise AssertionError(f"{description}: {value!r}")


def test_a_foreign_program_on_the_bridge_port_is_reported_and_the_waiting_message_runs_after_recovery(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    assistant_id = sid = box = ""
    marker = uuid.uuid4().hex
    # The stand-in for an older image's server: it answers every path 404.
    stand_in = (
        "import http.server,sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_error(404)\n"
        " def log_message(self,*a): pass\n"
        f"http.server.ThreadingHTTPServer(('0.0.0.0',{_BRIDGE_PORT}),H).serve_forever()  # {marker}\n"
    )
    try:
        environment, image, _digest, model, _kubeconfig, _namespace = _assistant_runtime(e2e_client)
        assistant_id = str(data(e2e_client.post("/api/v1/assistants", json={
            "display_name": f"__e2e_foreign_backend_{marker[:8]}",
            "runtime_profile_name": environment,
            "model_config_override": {"model_name": model},
        })).get("assistant_id") or "")
        assert assistant_id
        workspace = _wake_assistant(e2e_client, assistant_id, timeout=max(0.1, live_test_deadline - time.monotonic()))
        box = str(workspace["current_sandbox_id"])
        sid = str(data(e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={}))["session_id"])
        poll_until_ready(e2e_client, sid, expected_image=image)
        first = send_and_follow(e2e_client, sid, f"Reply with exactly {marker}_FIRST and nothing else. "
                                                 "Do not use any tool.", until="end")
        assert first.error is None and f"{marker}_FIRST" in first.text, first.error or first.text
        wait_until_settled(e2e_client, sid)

        # The bridge stops, so the reader's connection ends and its reattachment
        # waits for the port: nothing serves it yet. The message sent now waits
        # with the platform.
        _in_box(box, "supervisorctl stop astrabox-hermes-bridge")
        assert _health(box) == "000"
        waiting = str(send_input(e2e_client, sid, f"Reply with exactly {marker}_WAITING and nothing else. "
                                                   "Do not use any tool.")["input_id"])

        # Another program takes the port and answers 404.
        _in_box(box, f"nohup python3 -c {shlex.quote(stand_in)} >/dev/null 2>&1 & "
                     f"for i in $(seq 50); do [ \"$(curl -s -o /dev/null -m 1 -w '%{{http_code}}' {_HEALTH})\" = 404 ] "
                     "&& exit 0; sleep 0.1; done; exit 1")
        taken = time.monotonic()
        reported = _wait(lambda: get_session(e2e_client, sid),
                         lambda detail: detail.get("recovery_reason") == "runtime_protocol_incompatible",
                         "the conversation never reported the mismatch",
                         min(live_test_deadline, taken + _RECORDED_WITHIN_S))
        assert reported["state"] == "RECOVERY_REQUIRED", reported
        reason = str(reported.get("last_error") or "")
        assert "HTTP 404" in reason and "sandbox image" in reason, reason
        held = recorded_input(sid, waiting)
        assert held["state"] == "pending" and held["delivered_at"] is None, held

        # The bridge is back; recovering the conversation hands the message on.
        _in_box(box, f"pkill -f {_pattern(marker)}; supervisorctl start astrabox-hermes-bridge")
        _wait(lambda: _health(box), lambda code: code == "200", "the bridge never answered ready", live_test_deadline)
        recovered = data(e2e_client.post(f"/api/v1/sessions/{sid}/recover"))
        assert recovered.get("recovery_reason") in (None, ""), recovered

        delivered = _wait(lambda: recorded_input(sid, waiting), lambda row: bool(row["response_id"]),
                          "the waiting message was never delivered", live_test_deadline)
        answered = _wait(lambda: recorded_response(sid, delivered["response_id"]),
                         lambda row: row["status"] == "ended", "the waiting message was never answered",
                         live_test_deadline)
        assert answered["outcome"] == "completed" and answered["input_ids"] == [waiting], answered
        responses = psql(
            "SELECT r.response_id FROM session_responses r JOIN session_response_inputs i "
            "USING(session_id,response_id) "
            f"WHERE r.session_id={sql_literal(sid)} AND i.input_id={sql_literal(waiting)}"
        )
        assert responses == [answered["response_id"]], responses
        settled = wait_until_settled(e2e_client, sid)
        assert settled["state"] == "READY" and not settled.get("last_error"), settled
        history = data(e2e_client.get(f"/api/v1/sessions/{sid}/messages"))
        assert f"{marker}_WAITING" in str(history), "the answer to the waiting message is not in the history"
    finally:
        if box:
            subprocess.run([*_sandbox_exec(box), "bash", "-c",
                            f"pkill -f {_pattern(marker)}; supervisorctl start astrabox-hermes-bridge"],
                           capture_output=True, timeout=60, check=False)
        release_session(sid)
        release_assistant(assistant_id)
