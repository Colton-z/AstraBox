"""An Assistant's system prompt, from the API to the answer Hermes gives.

The system prompt is what the owner writes for who the Assistant is. For Hermes
it lands as ``SOUL.md``, the vendor's identity slot, which replaces the built-in
"You are Hermes Agent" text. So the observable outcome is the Assistant's own
account of who it is: the identity the owner configured while it is set, and
Hermes' own once the owner clears it.

Both directions are proven on a real model. Asking the name is the cheapest
question whose answer comes from the identity and nowhere else: the prompt
names the Assistant something no model would say on its own, and Hermes'
default names it Hermes. A system prompt that was stored but never reached the
program answers "Hermes" to the first question; a cleared one that the program
kept answers the configured name to the second.

Runs in the Assistant lane against a Hermes Environment:

    E2E_LANE=assistant \\
      ASTRABOX_E2E_ASSISTANT_ENVIRONMENT=<assistant environment> \\
      ASTRABOX_E2E_ASSISTANT_MODEL=<model> ./scripts/e2e_live.sh
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    StreamResult,
    data,
    get_session,
    poll_until_ready,
    release_assistant,
    release_session,
    stream_turn,
    wait_until_settled,
)

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live]

_NAME_QUESTION = "What is your name? Reply with only your name. Do not use any tool."


def _assistant_environment(e2e_client: httpx.Client) -> tuple[str, str, str | None]:
    """The Hermes Environment and model under test, and the image if pinned."""

    environment = os.getenv("ASTRABOX_E2E_ASSISTANT_ENVIRONMENT", "").strip()
    model = os.getenv("ASTRABOX_E2E_ASSISTANT_MODEL", "").strip()
    image = os.getenv("ASTRABOX_E2E_ASSISTANT_IMAGE", "").strip() or None
    assert environment and model, (
        "the Assistant lane requires ASTRABOX_E2E_ASSISTANT_ENVIRONMENT and "
        "ASTRABOX_E2E_ASSISTANT_MODEL"
    )
    matches = [
        item
        for item in data(e2e_client.get("/api/v1/admin/environments"))
        if item.get("name") == environment
    ]
    assert len(matches) == 1, f"expected one Environment {environment!r}: {matches}"
    assert matches[0].get("engine_kind") == "assistant", matches[0]
    return environment, model, image


def _wake(e2e_client: httpx.Client, assistant_id: str, *, deadline: float) -> None:
    last: dict = {}
    while time.monotonic() < deadline:
        last = data(
            e2e_client.post(
                f"/api/v1/assistants/{assistant_id}/workspace/wake", timeout=120.0
            )
        )
        if last.get("state") == "READY" and last.get("current_sandbox_id"):
            return
        time.sleep(3.0)
    pytest.fail(f"assistant {assistant_id} workspace did not become READY: {last}")


def _open_conversation(
    e2e_client: httpx.Client, assistant_id: str, image: str | None
) -> str:
    conversation = data(
        e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
    )
    session_id = str(conversation.get("session_id") or "")
    assert session_id, f"Assistant conversation returned no session: {conversation}"
    release_session(session_id)
    poll_until_ready(e2e_client, session_id, expected_image=image)
    return session_id


def _ask_name(
    e2e_client: httpx.Client, assistant_id: str, image: str | None
) -> str:
    """Open a new conversation, ask the name, and return the answer."""

    session_id = _open_conversation(e2e_client, assistant_id, image)
    answer = stream_turn(e2e_client, session_id, content=_NAME_QUESTION)
    assert answer.error is None, f"the turn errored: {answer.error}"
    assert answer.text.strip(), "the turn streamed no reply"
    wait_until_settled(e2e_client, session_id)
    return answer.text


def _create(
    e2e_client: httpx.Client, *, environment: str, model: str, system: str
) -> str:
    created = data(
        e2e_client.post(
            "/api/v1/assistants",
            json={
                "display_name": f"__e2e_system_prompt_{uuid.uuid4().hex[:8]}",
                "environment_name": environment,
                "model_config_override": {"model_name": model},
                "system": system,
            },
        )
    )
    assistant_id = str(created.get("assistant_id") or "")
    assert assistant_id, f"assistant create returned no id: {created}"
    release_assistant(assistant_id)
    stored = data(e2e_client.get(f"/api/v1/assistants/{assistant_id}"))
    assert stored.get("system") == system.strip(), (
        f"the Assistant did not keep its system prompt: {stored.get('system')!r}"
    )
    return assistant_id


def _identity(name: str) -> str:
    return (
        f"Your name is {name}. You are not Hermes and you never call yourself "
        f"Hermes. When anyone asks your name, answer with exactly {name}.\n"
    )


def test_the_system_prompt_is_the_assistants_identity(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    environment, model, image = _assistant_environment(e2e_client)
    name = f"Quillon-{uuid.uuid4().hex[:4].upper()}"
    assistant_id = _create(
        e2e_client, environment=environment, model=model, system=_identity(name)
    )
    _wake(e2e_client, assistant_id, deadline=live_test_deadline)

    answer = _ask_name(e2e_client, assistant_id, image)

    assert name.lower() in answer.lower(), (
        f"the Assistant did not answer as the identity its system prompt set "
        f"({name}): {answer[:200]!r}"
    )


def test_clearing_the_system_prompt_restores_the_programs_identity(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    environment, model, image = _assistant_environment(e2e_client)
    name = f"Quillon-{uuid.uuid4().hex[:4].upper()}"
    assistant_id = _create(
        e2e_client, environment=environment, model=model, system=_identity(name)
    )
    _wake(e2e_client, assistant_id, deadline=live_test_deadline)
    configured = _ask_name(e2e_client, assistant_id, image)
    assert name.lower() in configured.lower(), (
        f"precondition: the configured identity was not in effect: {configured[:200]!r}"
    )

    cleared = data(
        e2e_client.patch(f"/api/v1/assistants/{assistant_id}", json={"system": None})
    )
    assert cleared.get("system") is None, cleared

    answer = _ask_name(e2e_client, assistant_id, image)

    assert name.lower() not in answer.lower(), (
        f"the cleared system prompt still decides who the Assistant is: {answer[:200]!r}"
    )
    assert "hermes" in answer.lower(), (
        f"the Assistant did not return to Hermes' own identity: {answer[:200]!r}"
    )


def test_editing_the_assistant_never_ends_a_reply_in_progress(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    """An edit plus a new conversation leave a streaming reply alone.

    Conversation A is writing a long reply when the owner changes the system
    prompt and starts conversation B. Hermes reads the system prompt for each
    new session, so B must use the new identity straight away while A's reply
    runs to its normal end. Restarting Hermes to apply the edit would have
    ended A's reply mid-stream; waiting for A before B could start would leave
    B preparing for as long as A runs. Both are caught here: A must finish
    normally, and B must be ready while A is still replying.
    """

    environment, model, image = _assistant_environment(e2e_client)
    tag = uuid.uuid4().hex[:4].upper()
    before, after = f"Quillon-{tag}", f"Wrenlow-{tag}"
    assistant_id = _create(
        e2e_client, environment=environment, model=model, system=_identity(before)
    )
    _wake(e2e_client, assistant_id, deadline=live_test_deadline)
    conversation_a = _open_conversation(e2e_client, assistant_id, image)

    reply: dict[str, StreamResult] = {}

    def _write_long_reply() -> None:
        # Its own client: httpx does not share one connection pool across a
        # stream and the requests made beside it on another thread.
        with httpx.Client(
            base_url=e2e_client.base_url, headers=e2e_client.headers, timeout=30.0
        ) as client:
            reply["a"] = stream_turn(
                client,
                conversation_a,
                content=(
                    "Write an essay of about 2000 words on the history of "
                    "lighthouses, in plain prose with no headings. Do not use any tool."
                ),
            )

    writer = threading.Thread(target=_write_long_reply)
    writer.start()
    while str(get_session(e2e_client, conversation_a).get("state") or "") != "PROCESSING":
        assert writer.is_alive(), f"A's reply ended before it was seen running: {reply}"
        assert time.monotonic() < live_test_deadline, "A's turn never started running"
        time.sleep(0.5)

    data(
        e2e_client.patch(
            f"/api/v1/assistants/{assistant_id}", json={"system": _identity(after)}
        )
    )
    conversation_b = _open_conversation(e2e_client, assistant_id, image)
    b_ready_while_a_replies = writer.is_alive()

    writer.join(max(1.0, live_test_deadline - time.monotonic()))
    assert not writer.is_alive(), "A's reply did not finish within the test budget"
    a = reply["a"]
    assert a.error is None and a.finish_reason == "stop", (
        f"A's reply did not run to its normal end after the edit: finish="
        f"{a.finish_reason!r} error={a.error!r} after {len(a.text)} characters"
    )
    assert b_ready_while_a_replies, (
        "B only became ready after A's reply ended: the edit waited for, or "
        "restarted, the program serving A"
    )
    answer = stream_turn(e2e_client, conversation_b, content=_NAME_QUESTION)
    assert answer.error is None, f"B's turn errored: {answer.error}"
    assert after.lower() in answer.text.lower(), (
        f"B did not answer as the edited identity ({after}): {answer.text[:200]!r}"
    )
