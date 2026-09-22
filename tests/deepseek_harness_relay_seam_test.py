"""What the harness tells the relay, in its downlink vocabulary.

The gateway multiplexes every session's events onto one downlink. A run is a
turn on the conversation's own session — `turn/start` opens it, `turn/end` is
the vendor's end — and a child session's frames are child facts, not this
response's output. The link numbers nothing, so a record's position is the
client's own count.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.deepseek_harness_client import (
    DeepSeekHarnessEngineClient,
    _DshRelaySeam,
)
from astrabox.core.service.orchestrator.engine.resident_relay import CountedRecord

ROOT = "session-c4f2b2b8-8a80-4b34-be99-4ff7a63e042b"
CHILD = "session-child-11111111-2222-4333-8444-555555555555"


class _Link:
    is_live = True

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        raise AssertionError(f"unexpected call {method}")


def _seam() -> _DshRelaySeam:
    client = DeepSeekHarnessEngineClient(session_id="sess-1", link=_Link(), native_session_id=ROOT)
    return _DshRelaySeam(client)


def _event(
    session_id: str, event_type: str, *, seq: int = 1, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "rpcId": "",
        "type": "session/event",
        "payload": {
            "type": "session/event",
            "sessionId": session_id,
            "event": {"type": event_type, "seq": seq, "time": seq, "data": data or {}},
        },
    }


def _request(agent_id: str) -> dict[str, Any]:
    return {
        "eventId": "rpc-approval-1",
        "type": "waterfall",
        "event": "approval/request",
        "agentId": agent_id,
        "request": {
            "toolName": "write",
            "callId": "call_00",
            "reason": "writes outside the workspace",
        },
    }


# ── boundaries and identity ──────────────────────────────────────────────
def test_a_run_is_a_turn_on_the_conversations_own_session() -> None:
    seam = _seam()

    assert seam.starts_run(_event(ROOT, "turn/start", data={"turn": 1}))
    assert seam.settles_run(
        _event(ROOT, "turn/end", data={"turn": 1, "reason": {"kind": "completed"}})
    )
    assert not seam.starts_run(_event(ROOT, "turn/end", data={"turn": 1}))
    # A child session's turn is that child's business, not a run here.
    assert not seam.starts_run(_event(CHILD, "turn/start", data={"turn": 1}))
    assert not seam.settles_run(_event(CHILD, "turn/end", data={"turn": 1}))


def test_the_response_identity_is_the_harnesss_turn_index() -> None:
    assert _seam().response_id(_event(ROOT, "turn/start", data={"turn": 3}), 9) == f"{ROOT}:3"


def test_the_position_is_the_clients_own_count() -> None:
    assert _DshRelaySeam.sequence(CountedRecord(sequence=42, record={})) == 42


# ── what translates ──────────────────────────────────────────────────────
def test_the_conversations_own_step_translates() -> None:
    seam = _seam()

    frames = seam.translate(
        seam.new_translator(), _event(ROOT, "step/start", data={"turn": 1, "step": 1})
    )

    assert [f["type"] for f in frames] == ["start-step"]


def test_a_child_sessions_event_is_not_this_responses_output() -> None:
    seam = _seam()

    assert (
        seam.translate(
            seam.new_translator(), _event(CHILD, "step/start", data={"turn": 1, "step": 1})
        )
        == []
    )


def test_a_request_is_an_interaction_not_output() -> None:
    seam = _seam()

    frame = seam.interaction(_request(ROOT))

    assert frame is not None and frame["type"] == "interaction.request"
    assert frame["interactionId"] == "rpc-approval-1"
    assert seam.translate(seam.new_translator(), _request(ROOT)) == []


def test_a_strangers_request_is_nobodys() -> None:
    assert _seam().interaction(_request("session-someone-elses")) is None


# ── child facts ──────────────────────────────────────────────────────────
def test_parent_catalog_and_gateway_status_carry_child_facts() -> None:
    seam = _seam()

    assert not seam.carries_child_facts({"type": "emit", "event": "api-session/added", "args": [{}]})
    assert seam.carries_child_facts(_event(ROOT, "subagent/catalog", data={
        "version": 0, "childId": CHILD, "childCreatedAt": 1,
        "mode": "continuable", "label": "researcher",
    }))
    assert seam.carries_child_facts(
        {"type": "emit", "event": "api-session/status", "args": [CHILD, True]}
    )
    assert not seam.carries_child_facts(_event(ROOT, "step/start"))
    assert not seam.carries_child_facts(_event("session-someone-elses", "step/start"))
    assert not seam.carries_child_facts(_event("session-someone-elses", "subagent/catalog"))


@pytest.mark.asyncio
async def test_a_strangers_event_folds_to_nothing_and_nothing_is_journaled() -> None:
    """The harness's own catalog and history are a child's durable record, so
    the relay has no native record to journal for one."""

    seam = _seam()

    assert await seam.child_facts(_event("session-someone-elses", "turn/end")) == []
    assert seam.native_records() == []
    assert seam.owed_child_reads() == []
