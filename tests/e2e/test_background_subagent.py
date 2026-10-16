"""Child-run e2e — the stream's child-run parts, the durable projection, control,
and the response a runtime starts on its own when background work finishes.

Contract proven here:

* A response that launches a subagent carries the engine's ``data-subagent``
  parts on the session's stream. They are transient (the console refreshes the
  Session child-run resource on them) and carry no engine-private reference;
  ``/sessions/{id}/child-runs`` owns identity and lifecycle for every engine.
* Once the work is done the session settles and answers its next message.
* A live child run that declares the ``stop`` operation can be stopped by its
  public ``child_run_id``. Vendor task or thread ids never cross this API.
* When the supplier declares completion-triggered responses, background work
  finishing after the reply makes the runtime answer on its own: a response
  of origin ``engine``, answering no input, delivered by the console stream
  and retained in history, with no user message invented for it.

The prompts pin the subagent tool and background execution. If the model or
adapter does not exercise that contract, the test fails instead of turning a
missing tool call into a skip.

Run it explicitly (it is deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_background_subagent.py -m e2e -s
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    SessionStream,
    assert_child_run_control_unavailable,
    child_completion,
    contract_supported,
    create_session,
    data,
    deadline_after,
    engine_kind,
    follow_stream,
    history,
    instruction,
    message_text,
    permission_mode,
    poll_until_agent_ready,
    psql,
    recorded_response,
    release_session,
    send_and_follow,
    sql_literal,
    tool_name,
    user_messages,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e

#: Child-run fields that name the engine's own objects. The public projection
#: and the stream carry the platform's ``child_run_id`` instead.
_PRIVATE_CHILD_FIELDS = {
    "engine_ref",
    "parent_engine_ref",
    "control_ref",
    "engineRef",
    "parentEngineRef",
    "controlRef",
}


def _subagent_parts(stream: SessionStream) -> list[dict]:
    """Every ``data-subagent`` part the stream delivered, inside a response or not."""
    parts = [part for response in stream.responses.values() for part in response.parts]
    return [part for part in [*parts, *stream.outside] if part.get("type") == "data-subagent"]


def _list_child_runs(client: httpx.Client, sid: str) -> list[dict]:
    response = client.get(f"/api/v1/sessions/{sid}/child-runs", timeout=30.0)
    body = response.json()
    assert response.status_code == 200 and body.get("code") == "OK", (
        f"child-run projection failed for session={sid}: {response.status_code} {body}"
    )
    rows = (body.get("data") or {}).get("child_runs")
    assert isinstance(rows, list), f"child-run projection is not a list: {body}"
    return [dict(row) for row in rows if isinstance(row, dict)]


def _wait_for_child_runs(
    client: httpx.Client,
    sid: str,
    predicate,
    *,
    timeout: float,
) -> list[dict]:
    deadline = deadline_after(timeout)
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = _list_child_runs(client, sid)
        if predicate(last):
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"child-run projection did not reach the required state in time: {last}"
    )


#: How many times the launch is asked for before the model's refusal is the
#: answer. Whether a model calls a tool is its own decision and not a platform
#: contract, so the ask is repeated; a launch that DID happen and did not
#: project is a defect and fails on the first attempt, because the retry is
#: gated on the engine reporting no launch at all.
_LAUNCH_ATTEMPTS = 3


def _launch_background_subagent(
    client: httpx.Client,
    sid: str,
    *,
    content: str,
) -> Reply:
    """Ask for a background subagent until the ENGINE reports launching one.

    The ``data-subagent`` parts the stream carries are what says a launch
    happened, and an empty child-run projection has two causes that need
    telling apart: the platform never projected a live child, which is a
    defect worth a red build, or no child was ever launched, which is a model
    declining a tool call and points nowhere near the platform. Asking the
    stream first makes the refusal name which one it is.
    """

    attempts: list[str] = []
    for attempt in range(1, _LAUNCH_ATTEMPTS + 1):
        reply = send_and_follow(client, sid, content, until="end")
        assert reply.error is None, (
            f"the response failed before the background subagent surfaced: {reply.error}"
        )
        if _subagent_parts(reply.stream):
            return reply
        attempts.append(f"attempt {attempt}: parent tool calls={reply.tool_names}")
        wait_until_settled(client, sid)
    raise AssertionError(
        "the engine never reported launching a subagent, so there is nothing for "
        "the platform to project — the model answered without calling the tool. "
        f"{'; '.join(attempts)}"
    )


def _assert_subagent_parts_are_signals(stream: SessionStream) -> None:
    parts = _subagent_parts(stream)
    assert all(part.get("transient") is True for part in parts), (
        f"child-run stream parts must be transient: {parts}"
    )
    leaked = [
        part for part in parts
        if _PRIVATE_CHILD_FIELDS.intersection(part) or _PRIVATE_CHILD_FIELDS.intersection(part.get("data") or {})
    ]
    assert not leaked, f"child-run stream parts carry engine-private references: {leaked}"


def test_background_subagent_surfaces_on_stream_and_stays_sendable(
    e2e_client: httpx.Client,
) -> None:
    """A launched subagent reaches the stream and the projection; the session answers on."""
    suffix = uuid.uuid4().hex[:8]
    marker = f"SUBAGENT_LAUNCHED_{suffix}"
    # bypassPermissions so neither the subagent tool nor any tool the subagent runs is
    # held on a permission prompt — the subagent lifecycle is the behavior under test.
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("background_subagent"):
            assert_child_run_control_unavailable(e2e_client, sid)
            ordinary = send_and_follow(
                e2e_client,
                sid,
                "Reply with only the number 42. Do not use any tool.",
                until="end",
            )
            assert ordinary.error is None, ordinary.error
            assert _subagent_parts(ordinary.stream) == [], (
                "an engine without child runs emitted a child-run part: "
                f"{_subagent_parts(ordinary.stream)}"
            )
            assert _list_child_runs(e2e_client, sid) == []
            return

        reply = _launch_background_subagent(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('subagent')} tool to launch a subagent whose entire prompt is exactly: "
                "'Compute 6*7 and reply with only the number.'. Wait for that subagent to "
                f"finish, then reply to me with only this line: {marker}"
            ),
        )
        assert reply.saw_ui_header, "the session stream is not an AI SDK UI message stream"
        _assert_subagent_parts_are_signals(reply.stream)
        child_runs = _wait_for_child_runs(
            e2e_client,
            sid,
            lambda rows: any(row.get("closed") is True for row in rows),
            timeout=30.0,
        )
        assert all(str(row.get("child_run_id") or "").strip() for row in child_runs), (
            f"a projected child run is missing its public identity: {child_runs}"
        )
        assert all(isinstance(row.get("closed"), bool) for row in child_runs), (
            f"a projected child run is missing structural closure state: {child_runs}"
        )
        assert all(isinstance(row.get("operations"), list) for row in child_runs), (
            f"a projected child run is missing its operations: {child_runs}"
        )
        completed = [row for row in child_runs if row.get("closed") is True]
        expected_statuses, expected_reasons = child_completion()
        assert any(
            row.get("engine_kind") == engine_kind()
            and row.get("engine_status") in expected_statuses
            and (
                not expected_reasons
                or row.get("engine_reason") in expected_reasons
            )
            for row in completed
        ), (
            "the engine child did not close with a successful native outcome: "
            f"{child_runs}"
        )
        assert all(_PRIVATE_CHILD_FIELDS.isdisjoint(row) for row in child_runs), (
            f"child-run projection leaked engine-native references: {child_runs}"
        )

        settled = wait_until_settled(e2e_client, sid)
        assert str(settled.get("state")) == "READY", (
            f"session not READY after the subagent's work: {settled.get('state')!r}"
        )

        # The conversation answers its next message.
        follow = send_and_follow(
            e2e_client,
            sid,
            "Reply with a one-word greeting. Do not use any tool.",
            until="end",
        )
        assert follow.error is None, (
            f"the next message's response failed (session not sendable): {follow.error}"
        )
        assert follow.text.strip(), "the next message produced no assistant text"
        assert follow.finish_reason == "stop", (
            f"the next message's response did not end normally: {follow.finish_reason!r}"
        )
    finally:
        release_session(sid)


def test_background_subagent_stop_is_accepted(e2e_client: httpx.Client) -> None:
    """A live background child declares stop without exposing vendor ids."""
    suffix = uuid.uuid4().hex[:8]
    marker = f"BG_LAUNCHED_{suffix}"
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("background_subagent"):
            assert_child_run_control_unavailable(e2e_client, sid)
            assert _list_child_runs(e2e_client, sid) == []
            return

        _launch_background_subagent(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('subagent')} tool {instruction('controllable_child')} to launch a background "
                "subagent whose entire prompt is exactly: 'Run this exact bash command: "
                "sleep 120; echo BG_DONE'. Do not wait for it to finish. Immediately reply to "
                f"me with only this line: {marker}"
            ),
        )

        # The child projection is the cross-engine authority. A separate
        # background-task summary is an optional engine emission and cannot be
        # required before the child advertises its own current operations.
        child_runs = _wait_for_child_runs(
            e2e_client,
            sid,
            lambda rows: any("stop" in (row.get("operations") or []) for row in rows),
            timeout=30.0,
        )
        child_run_id = str(
            next(
                row for row in child_runs if "stop" in (row.get("operations") or [])
            ).get("child_run_id")
            or ""
        ).strip()
        assert child_run_id, f"controllable child run has no public identity: {child_runs}"

        # Public stop route resolves the adapter-owned control id server-side.
        resp = e2e_client.post(
            f"/api/v1/sessions/{sid}/child-runs/{child_run_id}/stop",
            timeout=30.0,
        )
        body = resp.json()
        assert resp.status_code == 200 and body.get("code") == "OK", (
            f"child-run stop was not accepted for child_run_id={child_run_id!r}: "
            f"{resp.status_code} {body}"
        )
        assert str((body.get("data") or {}).get("status") or "") == "accepted", (
            f"stop route did not report the child run accepted: {body}"
        )
        assert str((body.get("data") or {}).get("child_run_id") or "") == child_run_id, (
            f"stop route answered for a different child run: {body}"
        )
    finally:
        release_session(sid)


def test_a_background_task_finishing_after_the_reply_starts_an_engine_response(
    e2e_client: httpx.Client,
) -> None:
    """Work that finishes after the reply ended arrives as the runtime's own response.

    One message launches a background subagent whose command waits on a
    release file, and the reply ends while that child is held. Releasing the
    child lets it finish; the runtime is told and answers on its own. That
    answer is a response of origin ``engine`` naming no input: the console's
    open stream delivers it, history shows it after the reply, and no user
    message is invented for the runtime's own notification.
    """
    if not contract_supported("background_completion_response"):
        pytest.skip(
            "The selected supplier does not start a parent turn on child completion; "
            "Codex 0.160.0 records that notification without scheduling a turn."
        )
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        if not contract_supported("background_subagent"):
            assert_child_run_control_unavailable(e2e_client, sid)
            return

        token = uuid.uuid4().hex[:10].upper()
        launched, completed = f"LAUNCHED_{token}", f"COMPLETED_{token}"
        release_name = f"engine-response-{token}.release"
        release_path = workspace_path(e2e_client, sid, release_name)
        gate = "\n".join([
            "python3 - <<'PY'",
            "from pathlib import Path",
            "import time",
            f"release = Path({release_path!r})",
            "deadline = time.monotonic() + 120",
            "while not release.exists():",
            "    if time.monotonic() >= deadline:",
            "        raise TimeoutError('the test did not release the task')",
            "    time.sleep(0.2)",
            f"print({completed!r})",
            "PY",
        ])
        prompt = "\n".join([
            f"Use the {tool_name('subagent')} tool {instruction('controllable_child')} "
            "to launch exactly one subagent.",
            f"The subagent must use the {tool_name('command')} tool to run this exact command "
            "in the foreground, wait for it, and report its output:",
            gate,
            f"Immediately after launching it, reply only {launched} verbatim and end your reply.",
            "Do not wait for the subagent and do not run any command yourself.",
            f"When the subagent's completion notice arrives, send a new reply containing {completed} verbatim.",
            "Do not mention the completion token before that notice.",
        ])
        launch = send_and_follow(e2e_client, sid, prompt, until="end")
        assert launch.error is None and launch.finish_reason == "stop", (
            f"the launching reply did not end normally: {launch.error!r} {launch.finish_reason!r}"
        )
        assert launched in launch.text and completed not in launch.text, launch.text
        _wait_for_child_runs(
            e2e_client,
            sid,
            lambda rows: any(row.get("closed") is False for row in rows),
            timeout=60.0,
        )
        assert not launch.stream.from_engine(), (
            "the runtime answered on its own before the held child could finish: "
            f"{[r.summary() for r in launch.stream.from_engine()]}"
        )

        # The console's stream stays open from the end of the reply; the
        # runtime's own answer must arrive on it once the child finishes.
        released = data(
            e2e_client.post(
                f"/api/v1/sessions/{sid}/files/upload",
                data={"path": ""},
                files={"files": (release_name, b"release", "text/plain")},
            )
        )
        assert released.get("uploaded_count") == 1, released

        def answered_on_its_own(stream: SessionStream) -> bool:
            return any(
                r.ended and completed in r.text.upper() for r in stream.from_engine()
            )

        follow_stream(
            e2e_client,
            launch.stream,
            answered_on_its_own,
            describe=f"a response of origin 'engine' carrying {completed}",
        )
        own = next(r for r in launch.stream.from_engine() if completed in r.text.upper())
        assert own.input_ids == () and own.error is None and own.finish_reason == "stop", own.summary()
        assert own.metadata.get("origin") == "engine", own.metadata

        wait_until_settled(e2e_client, sid)
        record = recorded_response(sid, own.response_id)
        assert (record["origin"], record["input_ids"], record["outcome"]) == ("engine", [], "completed"), record
        inputs = psql(f"SELECT count(*) FROM session_inputs WHERE session_id = {sql_literal(sid)}")
        assert inputs == ["1"], f"an input was recorded for the runtime's own response: {inputs}"

        messages = history(e2e_client, sid)
        assert [m["id"] for m in user_messages(messages)] == [launch.client_message_id], (
            "history holds a user message the person never sent: "
            f"{[(m['id'], message_text(m)[:80]) for m in user_messages(messages)]}"
        )
        ids = [m["id"] for m in messages]
        assert own.response_id in ids and launch.response_id in ids, ids
        assert ids.index(own.response_id) > ids.index(launch.response_id), (
            f"history shows the runtime's own response before the reply it followed: {ids}"
        )
        shown = messages[ids.index(own.response_id)]
        assert shown["metadata"].get("origin") == "engine", shown["metadata"]
        assert shown["metadata"].get("input_ids") == [], shown["metadata"]
        assert shown["metadata"].get("outcome") == "completed", shown["metadata"]
        assert completed in message_text(shown).upper(), message_text(shown)
    finally:
        release_session(sid)
