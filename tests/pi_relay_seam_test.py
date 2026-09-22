"""What pi tells the relay, in pi's own words, and what the durable fold gives back.

The relay is engine-neutral; this is the pi side of it. Run boundaries are the
events pi documents (`agent_start` opens a run, `agent_settled` is the one
after which pi "will not continue running automatically"), a record's
position is the pipe's output offset, and the response identity is the native
session plus that offset. The adapter's durable fold is exercised with the
records the relay persists while nothing is running: a status snapshot and a
persisted inspect reply.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.pi import PiEngineAdapter
from astrabox.core.service.orchestrator.engine.pi_child_runs import (
    ASYNC_SNAPSHOT_PREFIX,
    INSPECT_REPLY_PREFIX,
    PiChildResources,
)
from astrabox.core.service.orchestrator.engine.pi_client import _PiRelaySeam
from astrabox.core.service.orchestrator.engine.pi_pipe import PiWireRecord

RUN = "800aa0b3-865c-402d-bec5-873e1eabca26"


def _client() -> Any:
    return SimpleNamespace(
        engine_session_key="01a08a09-3d58-71d5-8422-301254f31a21",
        _child_resources=PiChildResources(),
        _extension_command_id=None,
        _child_transcript=None,
    )


def _widget(state: str, *, children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = {
        "kind": "pi-subagents.async-status-snapshot",
        "version": 1,
        "runs": [{"id": RUN, "kind": "subagent", "label": "researcher", "state": state, "children": children or []}],
    }
    return {
        "type": "extension_ui_request",
        "id": "w-1",
        "method": "setWidget",
        "widgetKey": "subagent-async",
        "widgetLines": [ASYNC_SNAPSHOT_PREFIX + json.dumps(payload)],
    }


def _inspect(request_id: str, **fields: Any) -> dict[str, Any]:
    payload = {
        "kind": "pi-subagents.inspect-reply",
        "version": 1,
        "requestId": request_id,
        "asyncId": RUN,
        **fields,
    }
    return {
        "type": "extension_ui_request",
        "id": "i-1",
        "method": "setWidget",
        "widgetKey": "subagent-inspect",
        "widgetLines": [INSPECT_REPLY_PREFIX + json.dumps(payload, ensure_ascii=False)],
    }


# ── boundaries and identity ──────────────────────────────────────────────
def test_a_run_opens_at_agent_start_and_is_over_at_agent_settled() -> None:
    seam = _PiRelaySeam(_client())

    assert seam.starts_run({"type": "agent_start"})
    assert not seam.starts_run({"type": "turn_start"})
    assert seam.settles_run({"type": "agent_settled"})
    # pi may retry or drain a follow-up after agent_end; it is not the end.
    assert not seam.settles_run({"type": "agent_end"})


def test_a_records_position_is_the_pipes_output_offset() -> None:
    seam = _PiRelaySeam(_client())

    assert seam.sequence(PiWireRecord(record={"type": "agent_start"}, output_offset=4096)) == 4096


def test_the_response_identity_is_the_native_session_at_that_position() -> None:
    seam = _PiRelaySeam(_client())

    assert seam.response_id({"type": "agent_start"}, 4096) == (
        "01a08a09-3d58-71d5-8422-301254f31a21:4096"
    )


# ── what translates, what does not ───────────────────────────────────────
@pytest.mark.asyncio
async def test_the_sub_agent_widgets_are_child_facts_not_output() -> None:
    seam = _PiRelaySeam(_client())
    translator = seam.new_translator()

    assert seam.translate(translator, _widget("running")) == []
    facts = await seam.child_facts(_widget("running"))

    assert [f["data"]["event"] for f in facts] == ["opened"]
    assert seam.carries_child_facts(_widget("running"))
    # The record itself is what the durable fold reads back, once.
    assert seam.native_records() == [_widget("running")]
    assert seam.native_records() == []


@pytest.mark.asyncio
async def test_a_child_that_changed_owes_one_read_as_a_prompt_command() -> None:
    seam = _PiRelaySeam(_client())
    await seam.child_facts(_widget("running"))

    owed = seam.owed_child_reads()

    assert len(owed) == 1
    assert owed[0]["type"] == "prompt"
    assert owed[0]["message"].startswith(f"/subagents-inspect-rpc astrabox-1 {RUN}")
    assert seam.owed_child_reads() == []


def test_a_dialog_is_an_interaction_and_nothing_else() -> None:
    seam = _PiRelaySeam(_client())
    request = {"type": "extension_ui_request", "id": "d-1", "method": "confirm", "title": "Delete?"}

    frame = seam.interaction(request)
    assert frame is not None
    assert frame["type"] == "interaction.request"
    assert frame["interactionId"] == "d-1"
    assert seam.translate(seam.new_translator(), request) == []


def test_any_other_widget_stays_a_diagnostic() -> None:
    seam = _PiRelaySeam(_client())
    record = {"type": "extension_ui_request", "id": "s", "method": "setStatus", "text": "x"}

    frames = seam.translate(seam.new_translator(), record)

    assert [f["type"] for f in frames] == ["data-raw-event"]
    assert seam.interaction(record) is None


# ── the durable fold ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_late_single_step_alias_survives_the_resident_journal() -> None:
    seam = _PiRelaySeam(_client())
    await seam.child_facts(_widget("queued"))
    journal = seam.native_records()
    seam.owed_child_reads()
    await seam.child_facts(_inspect("astrabox-1", status="running"))
    journal.extend(seam.native_records())

    snapshot = _widget(
        "running",
        children=[{"id": "step:0", "kind": "step", "label": "researcher", "state": "running"}],
    )
    assert await seam.child_facts(snapshot) == []
    assert seam.native_records() == []
    assert seam.owed_child_reads() == [
        {"type": "prompt", "message": f"/subagents-inspect-rpc astrabox-2 {RUN} step:0 --lines 200"},
    ]
    reply = _inspect(
        "astrabox-2",
        childId="step:0",
        status="complete",
        messages=[{"role": "user", "kind": "text", "text": "Delegated task"}],
    )
    live = await seam.child_facts(reply)
    natives = seam.native_records()
    assert natives == [snapshot, reply]
    assert seam.native_records() == []
    journal.extend(natives)

    cold = [fact.as_frame() for _, fact in PiEngineAdapter().durable_child_resource_facts(journal)]
    assert [frame for frame in cold if frame["data"]["kind"] == "message"] == live
    assert not any(frame["data"].get("event") == "closed" for frame in cold)
    assert _PiRelaySeam(_client()).native_records() == []


@pytest.mark.asyncio
async def test_failed_child_inspection_is_journaled_without_poisoning_later_output() -> None:
    seam = _PiRelaySeam(_client())
    await seam.child_facts(_widget("running"))
    journal = seam.native_records()
    seam.owed_child_reads()
    error = {
        "code": "internal",
        "message": "Inspection could not read the async run artifacts.",
    }
    reply = _inspect("astrabox-1", error=error)
    diagnostic, = await seam.child_facts(reply)
    assert diagnostic["type"] == "data-raw-event"
    assert diagnostic["data"]["raw"]["error"] == error
    assert diagnostic["data"]["raw"]["requestId"] == "astrabox-1"
    journal.extend(seam.native_records())
    assert journal[-1] == reply
    assert seam.owed_child_reads() == []

    closed, = await seam.child_facts(_widget("failed"))
    assert closed["data"]["engineStatus"] == "failed"
    assert closed["data"]["event"] == "closed"
    journal.extend(seam.native_records())
    command, = seam.owed_child_reads()
    assert command["message"] == f"/subagents-inspect-rpc astrabox-2 {RUN} --lines 200"
    final_reply = _inspect(
        "astrabox-2", status="failed",
        messages=[{"role": "assistant", "kind": "text", "text": "Research is blocked: no web tools."}],
    )
    final, = await seam.child_facts(final_reply)
    assert final["data"]["content"] == [{"type": "text", "text": "Research is blocked: no web tools."}]
    journal.extend(seam.native_records())
    cold = [fact.as_frame() for _, fact in PiEngineAdapter().durable_child_resource_facts(journal)]
    assert [frame for frame in cold if frame["data"]["kind"] == "message"] == [final]
    assert [frame["data"]["engineStatus"] for frame in cold if frame["data"]["kind"] == "lifecycle"] == ["running", "failed"]


def test_the_adapter_folds_persisted_records_into_facts_in_order() -> None:
    """A snapshot, then a persisted reply to the read it owed — matched by the
    run the reply names, since no live request table survives to the fold."""

    facts = PiEngineAdapter().durable_child_resource_facts(
        [
            _widget("running"),
            _inspect(
                "astrabox-1",
                status="complete",
                finalOutput="Xiaomi: 2024 revenue up 35%.",
                messages=[{"role": "assistant", "kind": "text", "text": "Reading filings."}],
            ),
        ]
    )

    assert [(index, fact.as_frame()["data"]["kind"]) for index, fact in facts] == [
        (0, "lifecycle"),
        (1, "message"),
        (1, "message"),
        (1, "lifecycle"),
    ]
    closed = facts[-1][1].as_frame()["data"]
    assert closed["event"] == "closed"
    assert closed["engineStatus"] == "complete"


def test_a_persisted_reply_for_a_run_never_seen_is_dropped() -> None:
    facts = PiEngineAdapter().durable_child_resource_facts(
        [_inspect("astrabox-9", status="complete", finalOutput="orphan")]
    )

    assert facts == []


@pytest.mark.parametrize("record", [{"type": "agent_start"}, {"type": "message_end", "message": {}}])
def test_records_without_child_meaning_fold_to_nothing(record: dict[str, Any]) -> None:
    assert PiEngineAdapter().durable_child_resource_facts([record]) == []
