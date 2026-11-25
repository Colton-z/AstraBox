"""A conversation's first start that fails with a retryable error hands its message to one replacement.

A person starts a conversation and sends the first message while its runtime
is still starting. That start fails with ``SANDBOX_UNREACHABLE``, an error the
registry marks retryable, before it reaches the sandbox provider: the
``runtime_start_unreachable`` fault in ``astrabox/testing/e2e_faults.py``,
declared for an Agent only this test uses. ``docs/sessions.md`` gives such a
start the lost-box handling: the Session stays ``READY`` with
``runtime_unavailable``, and the waiting message goes to one replacement start.

* When the replacement starts, it answers the message: the same input, admitted
  once and answered by one response from the replacement's box. The message is
  never failed while the start is replaced, and the Session ends ``READY``.
* When the replacement fails too, the message fails with that start's
  ``SANDBOX_UNREACHABLE``. The Session stays ``READY`` with
  ``runtime_unavailable``, which a read shows as ``RECOVERY_REQUIRED``, rather
  than ``TERMINATED``; no response starts, and no third start is attempted.

The message has to be accepted while the first start is pending, as a person's
first message is while a start takes seconds. The fault fails the start within
milliseconds of the create, so the test holds the conversation's startup lock
(``coordination.startup_lock``, which input acceptance does not take) across
the create and the send, as a concurrent startup holder would, and releases it
once the message is accepted. The conversation is created with an
``Idempotency-Key``, as the console creates one, which is what makes its id
known before it exists.
"""

from __future__ import annotations

import contextlib
import json
import select
import subprocess
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    Reply,
    SessionStream,
    answer_in_history,
    data,
    follow_reply,
    get_session,
    history,
    output_position,
    psql_rows,
    recorded_input,
    recorded_output,
    release_agent,
    release_session,
    send_input,
    sql_literal,
    transcript_entries,
    user_messages,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e

_FAULT = "runtime_start_unreachable"

#: Arms, reads or clears one ``runtime_start_unreachable`` declaration in the
#: server's fault directory, the directory the server's hook consumes from.
_FAULT_PROGRAM = """
import json,pathlib,sys
from astrabox.testing.e2e_faults import _fault_base_path
name,action,agent,count=sys.argv[1:]
base=_fault_base_path()
assert base, 'the server does not arm E2E faults'
p=pathlib.Path(base+'.d')/(name+'.json')
if action=='clear': p.unlink(missing_ok=True)
elif action=='arm':
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'runtime_start_unreachable':int(count)},'match':{'agent_id':agent},'consumed':[]}
 t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.chmod(0o644);t.replace(p)
print(p.read_text() if p.exists() else '{}')
"""

#: Holds the startup lock of the conversation that ``(user, agent, key)``
#: creates, through the deployment's own coordination, until stdin closes.
#: It prints the session id the create will answer with once the lock is held.
_HOLD_PROGRAM = """
import asyncio,sys
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.core.service.orchestrator.session_lifecycle.service import _idempotent_session_id
from astrabox.persistence.provider import get_persistence
async def hold(user,agent,key):
    sid=_idempotent_session_id(user,agent,key)
    async with get_persistence().coordination.startup_lock(sid):
        print(sid,flush=True)
        await asyncio.get_running_loop().run_in_executor(None,sys.stdin.read)
asyncio.run(hold(*sys.argv[1:]))
"""


def _fault(name: str, action: str, *, agent_id: str = "", count: int = 0) -> dict:
    """The declaration file ``name`` after ``action`` (``arm``, ``read`` or ``clear``)."""
    return json.loads(subprocess.check_output([
        "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
        "python", "-c", _FAULT_PROGRAM, name, action, agent_id, str(count),
    ], text=True, timeout=15))


@contextlib.contextmanager
def _startup_held(user_id: str, agent_id: str, key: str, deadline: float) -> Iterator[str]:
    """Keep the conversation ``key`` creates from starting; yield its session id."""
    holder = subprocess.Popen(
        ["docker", "exec", "-i", require_service_container(SERVER_CONTAINER_HANDLE),
         "python", "-c", _HOLD_PROGRAM, user_id, agent_id, key],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert holder.stdout is not None
    ready, _, _ = select.select([holder.stdout], [], [], max(0.1, min(30.0, deadline - time.monotonic())))
    sid = holder.stdout.readline().strip() if ready else ""
    if not sid:
        holder.kill()
        _, err = holder.communicate(timeout=15)
        raise AssertionError(f"the startup lock was never held: {err[-1500:]}")
    try:
        yield sid
    finally:
        # Closing stdin ends the hold; the lock goes with the holder's transaction.
        try:
            _, err = holder.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            holder.kill()
            _, err = holder.communicate(timeout=15)
    assert holder.returncode == 0, f"the startup lock holder failed: {err[-1500:]}"


def _session_row(sid: str) -> dict:
    rows = psql_rows(
        "SELECT state, runtime_unavailable, runtime_start_failed_at, last_error_code, sandbox_id, "
        "sandbox_generation, undestroyed_sandbox_ids FROM sessions "
        f"WHERE session_id = {sql_literal(sid)}"
    )
    assert len(rows) == 1, f"session {sid} has {len(rows)} rows"
    return rows[0]


def _responses(sid: str) -> list[dict]:
    return psql_rows(
        "SELECT r.response_id, r.origin, r.status, r.outcome, "
        "COALESCE((SELECT array_agg(i.input_id ORDER BY i.input_id) FROM session_response_inputs i "
        "WHERE i.session_id = r.session_id AND i.response_id = r.response_id), '{}') AS input_ids "
        f"FROM session_responses r WHERE r.session_id = {sql_literal(sid)}",
        order_by="response_id",
    )


def _send_while_starting(
    client: httpx.Client, *, user_id: str, agent_id: str, prompt: str, deadline: float,
) -> tuple[Reply, str]:
    """Create a conversation and send ``prompt`` before its first start reaches the fault.

    Returns the message and the sandbox generation the first start was given.
    """
    key = f"retryable-start-{uuid.uuid4().hex}"
    with _startup_held(user_id, agent_id, key, deadline) as sid:
        started = data(client.post(
            f"/api/v1/agents/{agent_id}/conversations", json={}, headers={"Idempotency-Key": key},
        ))
        assert started["session_id"] == sid, f"the create made another conversation: {started}"
        release_session(sid)
        first_generation = str(_session_row(sid)["sandbox_generation"] or "")
        stream = SessionStream(sid, cursor=output_position(client, sid))
        receipt = send_input(client, sid, prompt)
        reply = Reply(
            session_id=sid, input_id=str(receipt["input_id"]),
            client_message_id=str(receipt["client_message_id"]), stream=stream,
        )
        # Accepted while the first start waits: nothing has consumed its fault yet.
        waiting = (
            get_session(client, sid)["state"],
            recorded_input(sid, reply.input_id)["state"],
            _fault(agent_id, "read")["consumed"],
        )
        assert waiting == ("CREATING", "pending", []), (
            f"the message was not accepted while the first start was pending: {waiting}"
        )
    return reply, first_generation


def _native_reply_holds(sid: str, token: str, prompt: str, deadline: float) -> list[dict]:
    """Native transcript entries that carry ``token`` and are not the message itself."""
    found: list[dict] = []
    while time.monotonic() < deadline:
        found = [
            entry for entry in transcript_entries(sid)
            if token in json.dumps(entry["entry"]) and prompt not in json.dumps(entry["entry"])
        ]
        if found:
            return found
        time.sleep(1.0)
    raise AssertionError(f"the runtime's own transcript never held its reply {token!r}")


def test_a_first_start_that_fails_retryably_hands_its_message_to_one_replacement(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    profile = current_profile()
    source = data(client.get(f"/api/v1/agents/{profile['agent_id']}"))
    payload = {
        "name": f"{profile['agent_name']} retryable start {uuid.uuid4().hex[:10]}",
        "model": profile["model"],
        "runtime_profile_name": profile["runtime_profile_name"],
        "engine_options": source.get("engine_options") or {},
        # Cold: the box that answers is one the replacement start creates,
        # not a prepared one either start could claim.
        "prepared_capacity": 0,
    }
    if isinstance(source.get("system"), str) and source["system"].strip():
        payload["system"] = source["system"]
    agent_id = str(data(client.post("/api/v1/agents", json=payload)).get("agent_id") or "")
    assert agent_id
    release_agent(agent_id)
    user_id = str(data(client.get("/api/v1/user/current"))["user_id"])
    evidence: dict = {"agent_id": agent_id}
    try:
        # ── one failed start: the replacement answers the waiting message ──
        _fault(agent_id, "arm", agent_id=agent_id, count=1)
        token = f"REPLACED_{uuid.uuid4().hex[:12].upper()}"
        prompt = f"Reply with exactly {token} and nothing else. Do not use any tool."
        reply, first_generation = _send_while_starting(
            client, user_id=user_id, agent_id=agent_id, prompt=prompt, deadline=live_test_deadline,
        )
        sid = reply.session_id

        # The first start fails and a replacement claims the conversation with
        # a new box generation (``SessionRecovery._reset``). The message waits
        # for it and is never failed on the way.
        recovering: dict = {}
        while not recovering:
            consumed = _fault(agent_id, "read")["consumed"]
            row = _session_row(sid)
            entry = recorded_input(sid, reply.input_id)
            assert entry["state"] != "failed", (
                f"the message failed while its conversation's first start was replaced: "
                f"{entry['failure']}; the Session reads {row['state']} ({row['last_error_code']})"
            )
            if consumed and row["sandbox_generation"] != first_generation:
                recovering = {"session": row, "input": entry}
                break
            assert time.monotonic() < live_test_deadline, (
                f"no replacement start was seen: consumed={consumed} session={row} input={entry}"
            )
            time.sleep(0.2)
        assert consumed == [{"fault": _FAULT, "session_id": sid, "agent_id": agent_id}], consumed

        follow_reply(client, reply, until="end")
        assert reply.response is not None and reply.response.input_ids == (reply.input_id,), (
            reply.stream.summary()
        )
        assert reply.error is None and token in reply.text, (reply.error, reply.text)
        settled = wait_until_settled(client, sid)
        assert settled["state"] == "READY" and not settled.get("runtime_unavailable"), settled

        row = _session_row(sid)
        assert row["state"] == "READY" and row["runtime_unavailable"] is False, row
        assert row["runtime_start_failed_at"] is None and row["last_error_code"] is None, row
        assert row["sandbox_id"] and row["sandbox_generation"] not in {None, first_generation}, row
        assert row["undestroyed_sandbox_ids"] == [], row
        assert _fault(agent_id, "read")["faults"] == {_FAULT: 0}

        # Admitted once and answered by the one response the stream showed.
        admitted = recorded_input(sid, reply.input_id)
        assert (admitted["state"], admitted["failure"], admitted["response_id"]) == (
            "admitted", None, reply.response_id,
        ), admitted
        assert _responses(sid) == [{
            "response_id": reply.response_id, "origin": "input", "status": "ended",
            "outcome": "completed", "input_ids": [reply.input_id],
        }], _responses(sid)
        recorded_text = "".join(
            str(part.get("delta") or "") for part in recorded_output(sid, str(reply.response_id))
            if part.get("type") == "text-delta"
        )
        assert recorded_text == reply.text, (recorded_text, reply.text)
        messages = history(client, sid)
        assert [m["id"] for m in user_messages(messages)] == [reply.client_message_id]
        answer = answer_in_history(messages, reply.input_id)
        assert answer["id"] == reply.response_id
        assert answer["metadata"].get("outcome") == "completed", answer["metadata"]
        native = _native_reply_holds(sid, token, prompt, live_test_deadline)
        evidence["transient"] = {
            "session_id": sid, "input_id": reply.input_id, "response_id": reply.response_id,
            "first_generation": first_generation, "replacement_generation": row["sandbox_generation"],
            "sandbox_id": row["sandbox_id"], "recovering": recovering, "consumed": consumed,
            "native_reply_entries": len(native),
        }
        _fault(agent_id, "clear")

        # ── the replacement fails too: the message fails with its error ──
        _fault(agent_id, "arm", agent_id=agent_id, count=3)
        failing, _first = _send_while_starting(
            client, user_id=user_id, agent_id=agent_id,
            prompt="Reply with one short sentence about the weather. Do not use any tool.",
            deadline=live_test_deadline,
        )
        lost = failing.session_id
        follow_reply(client, failing, until="end", allow_refusal=True)
        failure = failing.input_failure
        assert failing.response is None and failure is not None, failing.stream.summary()
        assert failure.get("code") == "SANDBOX_UNREACHABLE", failure

        # A third start would follow the failure within one reader sweep
        # (``_SWEEP_INTERVAL_S`` in ``session_runtime/host.py``, 5 s); watch two.
        quiet_until = time.monotonic() + 12.0
        while time.monotonic() < quiet_until:
            spent = _fault(agent_id, "read")
            row = _session_row(lost)
            assert len(spent["consumed"]) == 2 and row["state"] == "READY", (
                f"a start followed the replacement's failure: {spent} session={row}"
            )
            time.sleep(0.5)
        assert spent["faults"] == {_FAULT: 1}, spent
        assert spent["consumed"] == [{"fault": _FAULT, "session_id": lost, "agent_id": agent_id}] * 2, spent

        refused = recorded_input(lost, failing.input_id)
        assert refused["state"] == "failed" and refused["failure"]["code"] == "SANDBOX_UNREACHABLE", refused
        assert refused["response_id"] is None, refused
        assert row["runtime_unavailable"] is True and row["runtime_start_failed_at"], row
        assert row["last_error_code"] == "SANDBOX_UNREACHABLE" and row["sandbox_id"] is None, row
        # Stored READY without a runtime is what a read shows as
        # RECOVERY_REQUIRED (``SessionState``): open for the next message,
        # where a TERMINATED Session would have to be recovered first.
        detail = get_session(client, lost)
        assert (detail["state"], detail.get("runtime_unavailable"), detail.get("last_error_code")) == (
            "RECOVERY_REQUIRED", True, "SANDBOX_UNREACHABLE",
        ), detail
        assert _responses(lost) == [], _responses(lost)
        evidence["persistent"] = {
            "session_id": lost, "input_id": failing.input_id, "failure": failure,
            "session": row, "fault": spent,
        }
        print(json.dumps({"phase": "retryable-start-replacement-accepted", **evidence}, default=str), flush=True)
    finally:
        with contextlib.suppress(Exception):
            print(json.dumps({"fault_file": _fault(agent_id, "read"), **evidence}, default=str), flush=True)
        _fault(agent_id, "clear")
