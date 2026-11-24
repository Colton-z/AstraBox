"""A message to an archived channel conversation starts a new one.

Archiving a Session ends it: its runtime refuses new input with
``CONVERSATION_ENDED``. The channel keeps routing its external conversation to
that Session until a message arrives, so the message must replace the mapping
with a new Session of the same trigger and execution account, be committed
there once, and be answered, while the archived Session keeps its history.
Two messages arriving together at an ended mapping both land in one
replacement.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile,
    data,
    get_session,
    history,
    message_text,
    psql_rows,
    release_agent,
    release_session,
    sql_literal,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.channel_agent_capabilities_test import _READ, _RECIPIENT, _STOP
from tests.e2e.test_prepared_runtime_activation import _run_json

pytestmark = pytest.mark.e2e


def test_a_message_to_an_archived_channel_conversation_starts_a_new_one(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    profile = current_profile()
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    nonce = uuid.uuid4().hex
    log = f"/tmp/astrabox-e2e-channel-{nonce}.jsonl"
    me = data(client.get("/api/v1/user/current"))["user_id"]
    canonical = data(client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(client.post("/api/v1/agents", json={
        "name": f"Channel archive {nonce[:8]}",
        "model": profile["model"],
        "runtime_profile_name": profile["runtime_profile_name"],
        "prepared_capacity": 0,
        "engine_options": canonical.get("engine_options") or {},
    }))
    agent_id = agent["agent_id"]
    release_agent(agent_id)
    binding = data(client.post(f"/api/v1/admin/agents/{agent_id}/deployments", json={
        "scene": "channel:generic_json", "prompt_prefix": "",
    }))
    deployment_id = binding["deployment_id"]
    subprocess.run(["docker", "exec", "-d", server, "python", "-c", _RECIPIENT, log],
                   check=True, capture_output=True, timeout=30)
    try:
        while not (port := _run_json(["docker", "exec", server, "python", "-c", _READ, log + ".port"])):
            assert time.monotonic() < live_test_deadline, "the reply recipient did not start"
            time.sleep(0.1)
        callback = f"http://127.0.0.1:{port[0]['port']}/reply"

        def trigger(text: str) -> str:
            return data(client.post(f"/api/v1/deployments/{deployment_id}/trigger", headers={
                "x-channel-secret": binding["secret"],
            }, json={"text": text, "message_id": str(uuid.uuid4()), "conversation_id": nonce,
                     "reply": {"callback_url": callback}}))["session_id"]

        def replies() -> list[dict]:
            rows = _run_json(["docker", "exec", server, "python", "-c", _READ, log])
            assert isinstance(rows, list)
            return rows

        def wait_for(predicate, description: str):
            while time.monotonic() < live_test_deadline:
                if result := predicate():
                    return result
                time.sleep(0.2)
            pytest.fail(description)

        def answered(sid: str, count: int) -> None:
            """The Session settled and every reply it started reached the channel."""
            wait_until_settled(client, sid)
            wait_for(lambda: psql_rows(
                f"SELECT state FROM channel_outbox WHERE session_id={sql_literal(sid)}"
            ) and all(row["state"] == "DELIVERED" for row in psql_rows(
                f"SELECT state FROM channel_outbox WHERE session_id={sql_literal(sid)}"
            )), f"the replies of {sid} were not delivered")
            assert len(replies()) == count, replies()

        def inputs(sid: str) -> int:
            return psql_rows(f"SELECT count(*) AS n FROM session_inputs WHERE session_id={sql_literal(sid)}")[0]["n"]

        def mapped() -> list[dict]:
            return psql_rows(
                "SELECT session_id, execution_user_id FROM channel_conversations "
                f"WHERE deployment_id={sql_literal(deployment_id)} AND conversation_key={sql_literal(nonce)}"
            )

        def archive(sid: str) -> None:
            data(client.post(f"/api/v1/sessions/{sid}/archive", timeout=60))
            row = psql_rows(f"SELECT archived_at IS NOT NULL AS archived FROM sessions WHERE session_id={sql_literal(sid)}")
            assert row == [{"archived": True}], row

        def replacement(sid: str, archived: set[str]) -> None:
            assert sid not in archived, "the message was routed to an archived Session"
            session = get_session(client, sid)
            assert session["source_type"] == "channel" and session["user_id"] == me, session
            assert mapped() == [{"session_id": sid, "execution_user_id": ""}], mapped()

        first = trigger("Explain in one short sentence what a calendar is. Do not use tools.")
        release_session(first)
        answered(first, 1)
        first_history = [(row.get("id"), message_text(row)) for row in history(client, first)]
        archive(first)

        second = trigger("Explain in one short sentence what a clock is. Do not use tools.")
        release_session(second)
        replacement(second, {first})
        answered(second, 2)
        assert inputs(second) == 1
        assert inputs(first) == 1, "the archived Session received input"
        assert [(row.get("id"), message_text(row)) for row in history(client, first)] == first_history
        archive(second)

        # Both drives can find the archived mapping before either replaces it.
        with ThreadPoolExecutor(max_workers=2) as pool:
            together = list(pool.map(trigger, [
                "Explain in one short sentence what a map is. Do not use tools.",
                "Explain in one short sentence what a compass is. Do not use tools.",
            ]))
        for sid in together:
            release_session(sid)
        assert together[0] == together[1], together
        third = together[0]
        replacement(third, {first, second})
        wait_until_settled(client, third)
        assert inputs(third) == 2
        assert inputs(second) == 1, "the archived Session received input"
        wait_for(lambda: len(replies()) > 2, "no reply reached the channel after the concurrent messages")
        inbound = psql_rows(
            "SELECT state, session_id FROM channel_inbound "
            f"WHERE deployment_id={sql_literal(deployment_id)} ORDER BY created_at"
        )
        assert [row["session_id"] for row in inbound] == [first, second, third, third], inbound
        assert all(row["state"] == "SETTLED" for row in inbound), inbound
    finally:
        # A drive that lost the replacement race started a Session it did not
        # use; it is the test's too and goes with the Agent.
        for row in psql_rows(f"SELECT session_id FROM sessions WHERE agent_id={sql_literal(agent_id)}"):
            release_session(row["session_id"])
        subprocess.run(["docker", "exec", server, "python", "-c", _STOP, log],
                       capture_output=True, timeout=30)
