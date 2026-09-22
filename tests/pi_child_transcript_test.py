from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.engine.pi_child_runs import PiChildRunError
from astrabox.core.service.orchestrator.engine.pi_child_runs import ASYNC_SNAPSHOT_PREFIX
from astrabox.core.service.orchestrator.engine.pi_child_transcript import (
    STORED_CHILD_SOURCE,
    PiChildTranscriptCapture,
    stored_child_transcript_facts,
)

ROOT = "/home/conversation/.pi/sessions"
OWNER = f"{ROOT}/parent.jsonl"
STATUS_PATH = "/workspace/native-async/run/status.json"
TEMP_ROOT = "/home/conversation/.pi/subagents"


def _launch() -> dict:
    return {"role": "toolResult", "toolName": "subagent", "details": {
        "runId": "run", "asyncId": "run", "asyncDir": "/workspace/native-async/run",
        "mission": {"ownerSessionId": OWNER, "artifacts": [{"kind": "status", "path": STATUS_PATH}]},
    }}


def _status() -> dict:
    return {"runId": "run", "sessionId": OWNER, "state": "failed", "mode": "single", "steps": [
        {"sessionFile": f"{ROOT}/native-child/session.jsonl", "transcriptPath": f"{ROOT}/artifact.jsonl"},
    ]}


def _source(status: dict) -> dict:
    return {"type": STORED_CHILD_SOURCE, "source": "status", "status": status,
            "sessionRoot": ROOT, "ownerSessionFile": OWNER}


@pytest.mark.asyncio
@pytest.mark.parametrize("mission", [True, False])
async def test_native_history_launch_restores_status_linkage_without_a_new_model_turn(mission):
    status = _status()
    files = SimpleNamespace(read_file=AsyncMock(return_value=json.dumps(status)))
    sink = SimpleNamespace(persist_event=AsyncMock())
    capture = PiChildTranscriptCapture(filesystem=files, session_root=ROOT, temp_root=TEMP_ROOT,
                                      sink=sink, owner_session_file=OWNER)
    launch = _launch()
    if not mission:
        launch["details"].pop("mission")
    assert await capture.observe({"type": "message", "message": launch}) == []
    assert await capture.observe(launch) == []
    files.read_file.assert_called_with(STATUS_PATH)
    sink.persist_event.assert_awaited_once()
    stored = sink.persist_event.call_args.kwargs["payload"]["message"]
    assert stored["status"] == status
    assert stored["sessionRoot"] == ROOT
    assert stored["ownerSessionFile"] == OWNER


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "foreign-run", "foreign-owner"])
async def test_status_read_errors_are_diagnostics_not_relay_failures(failure):
    status = _status()
    if failure == "foreign-run":
        status["runId"] = "other"
    if failure == "foreign-owner":
        status["sessionId"] = "/other/session.jsonl"
    files = SimpleNamespace(read_file=AsyncMock(return_value=json.dumps(status)))
    if failure == "read":
        files.read_file.side_effect = OSError("supplier file unavailable")
    sink = SimpleNamespace(persist_event=AsyncMock())
    capture = PiChildTranscriptCapture(filesystem=files, session_root=ROOT, temp_root=TEMP_ROOT,
                                      sink=sink, owner_session_file=OWNER)
    diagnostics = await capture.observe(_launch())
    assert len(diagnostics) == 1
    assert diagnostics[0]["data"]["subtype"] == "child-transcript-read-failed"
    sink.persist_event.assert_not_called()


@pytest.mark.asyncio
async def test_native_snapshot_discovers_async_worker_history_without_a_parent_launch_result():
    status = _status()
    status["state"] = "running"
    files = SimpleNamespace(read_file=AsyncMock(return_value=json.dumps(status)))
    sink = SimpleNamespace(persist_event=AsyncMock())
    capture = PiChildTranscriptCapture(filesystem=files, session_root=ROOT, temp_root=TEMP_ROOT,
                                      sink=sink, owner_session_file=OWNER)
    snapshot = {"method": "setWidget", "widgetKey": "subagent-async", "widgetLines": [
        ASYNC_SNAPSHOT_PREFIX + json.dumps({"kind": "pi-subagents.async-status-snapshot", "version": 1,
            "runs": [{"id": "run", "kind": "subagent", "state": "running", "updatedAt": 1,
                      "children": [{"id": "step:0", "kind": "step", "state": "running"}]}]}),
    ]}
    assert await capture.observe(snapshot) == []
    files.read_file.assert_awaited_once_with(f"{TEMP_ROOT}/async-subagent-runs/run/status.json")
    stored = sink.persist_event.call_args.kwargs["payload"]["message"]
    assert stored["status"] == status
    scopes = [{"subpath": "pi/native-child/session.jsonl", "entries": [
        {"type": "message", "id": "task", "message": {"role": "user", "content": "actual worker task"}},
        {"type": "message", "id": "reply", "message": {"role": "assistant", "content": "real running answer " * 100}},
    ]}]
    facts = stored_child_transcript_facts(engine_ref="run", closed=False, raw_scopes=scopes, raw_messages=[stored])
    assert [fact["data"]["content"] for fact in facts] == [
        [{"type": "text", "text": "actual worker task"}],
        [{"type": "text", "text": "real running answer " * 100}],
    ]
    assert await capture.observe(snapshot) == []
    files.read_file.assert_awaited_once()
    sink.persist_event.assert_awaited_once()
    assert await capture.observe(_launch()) == []
    files.read_file.assert_awaited_with(STATUS_PATH)


def test_database_native_history_retains_full_prompt_and_acceptance_report_not_artifact_preview():
    prompt = "actual unredacted user task"
    final = "完整回答" * 600 + '\n```acceptance-report\n{"passed":true}\n```'
    scopes = [{"subpath": "pi/native-child/session.jsonl", "entries": [
        {"type": "session", "id": "native-child"},
        {"type": "message", "id": "user-1", "message": {"role": "user", "content": prompt}},
        {"type": "message", "id": "answer", "message": {"role": "assistant", "stopReason": "stop", "content": [
            {"type": "thinking", "thinking": "native reasoning"}, {"type": "text", "text": final},
        ]}},
    ]}, {"subpath": "pi/artifact.jsonl", "entries": [{"recordType": "message", "text": "[prompt redacted]"}]}]
    def read():
        return stored_child_transcript_facts(
            engine_ref="run", closed=True, raw_scopes=scopes, raw_messages=[_source(_status())]
        )
    facts = read()
    assert [fact["data"]["role"] for fact in facts] == ["user", "assistant"]
    assert facts[0]["data"]["content"] == [{"type": "text", "text": prompt}]
    assert facts[1]["data"]["content"] == scopes[0]["entries"][2]["message"]["content"]
    assert [fact.as_frame() for fact in read()] == [fact.as_frame() for fact in facts]
    assert all(fact["data"]["kind"] == "message" for fact in facts)


@pytest.mark.parametrize("is_error", [False, True])
def test_stored_tool_call_gains_its_native_result_without_changing_message_identity(is_error):
    arguments = {"command": "printf 'child-tool-output'", "timeout": 30}
    call = {"type": "toolCall", "id": "native-call", "name": "bash", "arguments": arguments}
    reasoning = {"type": "thinking", "thinking": "Inspect the child workspace."}
    scope = {"subpath": "pi/native-child/session.jsonl", "entries": [
        {"type": "message", "id": "request", "message": {
            "role": "assistant", "content": [reasoning, call], "stopReason": "toolUse",
        }},
    ]}

    def read(closed):
        return stored_child_transcript_facts(
            engine_ref="run", closed=closed, raw_scopes=[scope], raw_messages=[_source(_status())],
        )

    running = read(False)
    assert running[0]["data"]["content"] == [reasoning, {
        "type": "tool_use", "id": "native-call", "name": "bash", "input": arguments,
    }]
    output = [{"type": "text", "text": "child-tool-output" if not is_error else "user interrupted the session"},
              {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}]
    details = {"exitCode": 1 if is_error else 0, "nativeDetail": {"retained": True}}
    scope["entries"].extend([
        {"type": "message", "id": "result", "message": {
            "role": "toolResult", "toolCallId": "native-call", "toolName": "bash",
            "content": output, "details": details, "isError": is_error,
        }},
        {"type": "message", "id": "answer", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Child finished."}],
        }},
    ])
    completed = read(True)
    assert completed[0].as_frame() == running[0].as_frame()
    assert completed[1]["data"]["role"] == "assistant"
    assert completed[1]["data"]["content"] == [{
        "type": "tool_result", "tool_use_id": "native-call",
        "content": json.dumps(scope["entries"][1]["message"], ensure_ascii=False),
        "is_error": is_error,
        "tool_result_state": "output-error" if is_error else "output-available",
    }]
    assert json.loads(completed[1]["data"]["content"][0]["content"]) == scope["entries"][1]["message"]
    assert [fact["data"]["messageId"] for fact in completed] == [
        f"pi/native-child/session.jsonl:{entry_id}" for entry_id in ["request", "result", "answer"]
    ]
    assert [fact.as_frame() for fact in read(True)] == [fact.as_frame() for fact in completed]
    assert call == {"type": "toolCall", "id": "native-call", "name": "bash", "arguments": arguments}


@pytest.mark.parametrize(("message", "error"), [
    ({"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {}}]}, "lacks id or name"),
    ({"role": "assistant", "content": [{"type": "toolCall", "id": "call", "name": "bash", "arguments": "{}"}]}, "arguments are not an object"),
    ({"role": "toolResult", "content": [], "isError": False}, "lacks toolCallId"),
    ({"role": "toolResult", "toolCallId": "call", "content": []}, "boolean isError"),
])
def test_stored_tool_messages_do_not_invent_missing_native_identity_or_outcome(message, error):
    with pytest.raises(PiChildRunError, match=error):
        stored_child_transcript_facts(
            engine_ref="run", closed=True, raw_messages=[_source(_status())],
            raw_scopes=[{"subpath": "pi/native-child/session.jsonl", "entries": [
                {"type": "message", "id": "invalid", "message": message},
            ]}],
        )


def test_workflow_step_uses_native_key_and_does_not_leak_sibling_history():
    status = _status()
    status["mode"] = "workflow"
    status["steps"] = [
        {"workflowKey": "left", "sessionFile": f"{ROOT}/left.jsonl"},
        {"workflowKey": "right", "sessionFile": f"{ROOT}/right.jsonl"},
    ]
    scopes = [{"subpath": f"pi/{name}.jsonl", "entries": [
        {"type": "message", "id": "same-id", "message": {"role": "assistant", "content": name}},
    ]} for name in ["left", "right"]]
    snapshot = {"method": "setWidget", "widgetKey": "subagent-async", "widgetLines": [
        ASYNC_SNAPSHOT_PREFIX + json.dumps({"kind": "pi-subagents.async-status-snapshot", "version": 1,
            "runs": [{"id": "run", "kind": "workflow"}]}),
    ]}
    args = {"closed": True, "raw_scopes": scopes, "raw_messages": [snapshot, _source(status)]}
    assert stored_child_transcript_facts(engine_ref="run", **args) == []
    facts = stored_child_transcript_facts(engine_ref="run/right", **args)
    assert len(facts) == 1
    assert facts[0]["data"]["content"] == [{"type": "text", "text": "right"}]
    status["steps"] = status["steps"][:1]
    assert stored_child_transcript_facts(engine_ref="run", **args) == []


def test_foreground_result_uses_native_result_index_not_array_position():
    record = {"type": STORED_CHILD_SOURCE, "source": "tool-result", "sessionRoot": ROOT,
              "details": {"runId": "run", "results": [
                  {"index": 4, "sessionFile": f"{ROOT}/child.jsonl"},
              ]}}
    facts = stored_child_transcript_facts(
        engine_ref="run/step:4", closed=True, raw_messages=[record],
        raw_scopes=[{"subpath": "pi/child.jsonl", "entries": [
            {"type": "message", "id": "answer", "message": {"role": "assistant", "content": "exact child"}},
        ]}],
    )
    assert len(facts) == 1
    assert facts[0]["data"]["content"] == [{"type": "text", "text": "exact child"}]


def test_foreground_workflow_resolves_worker_history_by_native_keys_not_result_order():
    details = {"mode": "workflow", "runId": "workflow-call", "workflowChildren": {"children": [
        {"childId": "left", "runId": "left-worker", "state": "completed"},
        {"childId": "right", "runId": "right-worker", "state": "completed"},
    ]}, "results": [
        {"index": 0, "workflowKey": "right", "sessionFile": f"{ROOT}/right.jsonl"},
        {"index": 0, "workflowKey": "left", "sessionFile": f"{ROOT}/left.jsonl"},
    ]}
    source = {"type": STORED_CHILD_SOURCE, "source": "tool-result", "sessionRoot": ROOT, "details": details}
    scopes = [{"subpath": f"pi/{key}.jsonl", "entries": [
        {"type": "message", "id": "prompt", "message": {"role": "user", "content": f"actual {key} task"}},
        {"type": "message", "id": "reply", "message": {"role": "assistant", "content": f"full {key} reply " * 200}},
    ]} for key in ["left", "right"]]
    args = {"closed": True, "raw_scopes": scopes, "raw_messages": [source]}
    assert stored_child_transcript_facts(engine_ref="workflow-call", **args) == []
    for key in ["left", "right"]:
        facts = stored_child_transcript_facts(engine_ref=f"{key}-worker", **args)
        assert [fact["data"]["content"] for fact in facts] == [
            [{"type": "text", "text": f"actual {key} task"}],
            [{"type": "text", "text": f"full {key} reply " * 200}],
        ]
        assert all(fact["data"]["engineRef"] == f"{key}-worker" and "parentEngineRef" not in fact["data"] for fact in facts)
        assert [fact.as_frame() for fact in stored_child_transcript_facts(engine_ref=f"{key}-worker", **args)] == [fact.as_frame() for fact in facts]
    details["results"][0]["workflowKey"] = "unknown"
    with pytest.raises(PiChildRunError, match="no unique native child run"):
        stored_child_transcript_facts(engine_ref="right-worker", **args)


@pytest.mark.parametrize("closed", [False, True])
def test_workflow_status_links_the_separately_published_native_worker_to_its_full_session(closed):
    worker = "native-worker"
    status = _status()
    status.update(runId="workflow", mode="workflow", state="complete" if closed else "running")
    status["steps"][0].update(runId=worker, workflowKey="child")
    snapshot = {"method": "setWidget", "widgetKey": "subagent-async", "widgetLines": [
        ASYNC_SNAPSHOT_PREFIX + json.dumps({"kind": "pi-subagents.async-status-snapshot", "version": 1,
            "runs": [
                {"id": worker, "kind": "subagent", "children": [{"id": "step:0", "kind": "step"}]},
                {"id": "workflow", "kind": "workflow", "children": [{"id": "child", "kind": "step"}]},
            ]}),
    ]}
    content = [{"type": "text", "text": "actual delegated task"},
               {"type": "text", "text": "complete native reply " * 150}]
    scope = {"subpath": "pi/native-child/session.jsonl", "entries": [
        {"type": "message", "id": "user", "message": {"role": "user", "content": [content[0]]}},
        {"type": "message", "id": "reply", "message": {"role": "assistant", "content": [content[1]]}},
    ]}
    args = {"closed": closed, "raw_scopes": [scope], "raw_messages": [_source(status), snapshot]}
    assert stored_child_transcript_facts(engine_ref="workflow", **args) == []
    facts = stored_child_transcript_facts(engine_ref=worker, **args)
    assert [fact["data"]["content"] for fact in facts] == [[content[0]], [content[1]]]
    assert all(fact["data"]["engineRef"] == worker and "parentEngineRef" not in fact["data"] for fact in facts)
    assert [fact.as_frame() for fact in stored_child_transcript_facts(engine_ref=worker, **args)] == [fact.as_frame() for fact in facts]
    with pytest.raises(PiChildRunError, match="no stored native transcript linkage"):
        stored_child_transcript_facts(engine_ref="unrelated-worker", **{**args, "closed": True})
    status["steps"].append({**status["steps"][0], "sessionFile": f"{ROOT}/other.jsonl"})
    with pytest.raises(PiChildRunError, match="multiple steps"):
        stored_child_transcript_facts(engine_ref=worker, **args)


@pytest.mark.parametrize("missing", ["link", "session-file", "scope"])
def test_closed_child_requires_its_complete_native_source(missing):
    status = _status()
    if missing == "session-file":
        status["steps"] = []
    messages = [] if missing == "link" else [_source(status)]
    with pytest.raises(PiChildRunError, match="no stored|no native|missing or ambiguous"):
        stored_child_transcript_facts(engine_ref="run", closed=True, raw_scopes=[], raw_messages=messages)
    assert stored_child_transcript_facts(engine_ref="run", closed=False, raw_scopes=[], raw_messages=messages) == []
