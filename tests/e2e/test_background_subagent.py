"""Child-run e2e — transient invalidation, durable projection, and control.

This is the API/SSE proof of the "Agents panel" behavior: a turn that launches a
subagent through the ``Task`` tool must notify the live ``ai-stream`` that the
Session child-run resource changed, and the session must remain usable afterwards.

Contract proven here:

* A turn that launches a subagent emits transient
  ``data-child-runs-changed`` invalidations. The durable
  ``/sessions/{id}/child-runs`` resource owns identity and lifecycle for every
  engine; the stream carries no child or provider identity.
* Once the turn settles the session returns to ``READY`` and stays **sendable** —
  a short follow-up turn runs to completion and produces assistant text.
* A live child run that declares the ``stop`` operation can be stopped by its public
  ``child_run_id``. Vendor task or thread ids never cross this API.

The prompts pin ``Task`` and background execution. If the model or adapter does
not exercise that contract, the test fails instead of turning a missing tool call
into a skip.

The ``StreamResult`` reader in ``_sandbox_helpers`` does not retain child-run
invalidations, so this file reads the raw SSE the same way ``stream_turn`` does.

Run it explicitly (it is deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_background_subagent.py -m e2e -s
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    STREAM_TIMEOUT_S,
    assert_child_run_control_unavailable,
    child_completion,
    contract_supported,
    create_session,
    engine_kind,
    instruction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    stream_turn,
    tool_name,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e

@dataclass
class _SubagentStream:
    """The subset of one ``ai-stream`` POST needed to observe child invalidation.

    ``child_run_signals`` holds transient ``data-child-runs-changed`` frames;
    the Session resource is authoritative.
    ``tool_names`` are the ``tool-input-available`` tool names
    (used only for diagnostics — to show whether the parent invoked ``Task``).
    ``finish_reason`` / ``error`` / ``saw_ui_header`` mirror the base SSE reader.
    """

    child_run_signals: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    error: str | None = None
    saw_ui_header: bool = False


def _stream_turn_capturing_child_run_signals(
    client: httpx.Client,
    sid: str,
    *,
    content: str,
    timeout: float = STREAM_TIMEOUT_S,
) -> _SubagentStream:
    """POST ``ai-stream`` and retain transient child-run invalidations.

    Mirrors ``_sandbox_helpers.stream_turn`` exactly (same endpoint, headers, ``data:``
    line handling, JSON decode, timeout) but keeps the invalidations the base reader
    drops. Fails loud if
    the endpoint does not return an event stream.
    """
    res = _SubagentStream()
    start = time.monotonic()
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/ai-stream",
        json={"content": content, "client_message_id": str(uuid.uuid4())},
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(timeout, connect=30.0),
    ) as resp:
        content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in content_type, (
            f"ai-stream did not return SSE (content-type={content_type!r}); "
            f"body={resp.read()[:400]!r}"
        )
        res.saw_ui_header = resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"
        for raw in resp.iter_lines():
            # Wall-clock guard. The server keeps the SSE open with a 15s keepalive
            # comment, which resets the per-read timeout — so a wedged/quiet turn
            # would otherwise loop here forever. Stop reading after `timeout`
            # wall-seconds; the caller treats a frame-less capture as a failure.
            if time.monotonic() - start > timeout:
                break
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "data-child-runs-changed":
                res.child_run_signals.append(ev)
            elif etype == "tool-input-available":
                name = ev.get("toolName")
                if name:
                    res.tool_names.append(str(name))
            elif etype == "finish":
                res.finish_reason = str(ev.get("finishReason") or "")
            elif etype == "error":
                res.error = str(ev.get("errorText") or "unknown error")
    return res


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
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = _list_child_runs(client, sid)
        if predicate(last):
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"child-run projection did not reach the required state within {timeout:.0f}s: {last}"
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
) -> _SubagentStream:
    """Ask for a background subagent until the ENGINE reports launching one.

    The child-run signals the stream carries are what says a launch happened,
    and an empty child-run projection has two causes that need telling apart:
    the platform never projected a live child, which is a defect worth a red
    build, or no child was ever launched, which is a model declining a tool
    call and points nowhere near the platform. Asking the stream first makes
    the refusal name which one it is.
    """

    attempts: list[str] = []
    for attempt in range(1, _LAUNCH_ATTEMPTS + 1):
        res = _stream_turn_capturing_child_run_signals(client, sid, content=content)
        assert res.error is None, (
            f"turn errored before the background subagent surfaced: {res.error}"
        )
        if res.child_run_signals:
            return res
        attempts.append(f"attempt {attempt}: parent tool calls={res.tool_names}")
    raise AssertionError(
        "the engine never reported launching a subagent, so there is nothing for "
        "the platform to project — the model answered without calling the tool. "
        f"{'; '.join(attempts)}"
    )


def test_background_subagent_surfaces_on_stream_and_stays_sendable(
    e2e_client: httpx.Client,
) -> None:
    """create -> child resource changes -> root session stays sendable."""
    suffix = uuid.uuid4().hex[:8]
    marker = f"SUBAGENT_LAUNCHED_{suffix}"
    # bypassPermissions so neither the Task tool nor any tool the subagent runs is
    # held on a permission prompt — the subagent lifecycle is the behavior under test.
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("background_subagent"):
            assert_child_run_control_unavailable(e2e_client, sid)
            ordinary = _stream_turn_capturing_child_run_signals(
                e2e_client,
                sid,
                content="Reply with only the number 42. Do not use any tool.",
            )
            assert ordinary.error is None, ordinary.error
            assert ordinary.child_run_signals == [], (
                "an engine without child runs emitted a child invalidation: "
                f"{ordinary.child_run_signals}"
            )
            assert _list_child_runs(e2e_client, sid) == []
            return

        res = _launch_background_subagent(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('subagent')} tool to launch a subagent whose entire prompt is exactly: "
                "'Compute 6*7 and reply with only the number.'. Wait for that subagent to "
                f"finish, then reply to me with only this line: {marker}"
            ),
        )
        assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
        assert all(signal.get("transient") is True for signal in res.child_run_signals), (
            f"child-run stream signals must be transient invalidations: {res.child_run_signals}"
        )
        assert all(
            set(signal) == {"type", "transient", "data"}
            and set(dict(signal.get("data") or {})) == {"frameSeq"}
            and isinstance(dict(signal.get("data") or {}).get("frameSeq"), int)
            for signal in res.child_run_signals
        ), f"child-run invalidation leaked projection or provider fields: {res.child_run_signals}"
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
        private_names = {
            "engine_ref",
            "parent_engine_ref",
            "control_ref",
            "engineRef",
            "parentEngineRef",
            "controlRef",
        }
        assert all(private_names.isdisjoint(row) for row in child_runs), (
            f"child-run projection leaked engine-native references: {child_runs}"
        )

        # The turn settles back to a usable session (READY, no pending interaction).
        settled = wait_until_settled(e2e_client, sid)
        assert str(settled.get("state")) == "READY", (
            f"session not READY after the subagent turn: {settled.get('state')!r}"
        )

        # Sendable: a short follow-up turn runs to completion and produces text.
        follow = stream_turn(
            e2e_client,
            sid,
            content="Reply with a one-word greeting. Do not use any tool.",
        )
        assert follow.error is None, (
            f"follow-up turn errored (session not sendable): {follow.error}"
        )
        assert follow.text.strip(), "follow-up produced no assistant text — session not sendable"
        assert follow.finish_reason in ("stop", None), (
            f"follow-up did not finish cleanly (session not sendable): {follow.finish_reason!r}"
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

        res = _launch_background_subagent(
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
