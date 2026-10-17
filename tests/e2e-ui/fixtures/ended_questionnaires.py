"""Record synthetic native form contracts through the deployed reader lease."""
from __future__ import annotations

import asyncio
import json
import sys

from astrabox.deploy.onebox import ensure_database_wiring

ensure_database_wiring()

from astrabox.core.service.orchestrator.engine.claude_runtime_client import interaction_record as claude_form
from astrabox.core.service.orchestrator.engine.codex_runtime_client import interaction_record as codex_form
from astrabox.core.service.orchestrator.engine.interaction_contract import validate_interaction_response
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch, InteractionClose, InteractionOpen, OutputPart, ResponseEnd, ResponseStart,
    SessionRuntimeRepository,
)


async def main(scene: dict) -> None:
    sessions, facts = SessionRepository(), SessionRuntimeRepository()
    result = []
    try:
        for status, sid in scene["sessions"].items():
            response_id, tool_id = f"response-{sid}", f"tool-{sid}"
            native_questions = [
                {"id": "single", "header": "Single", "question": "Choose one colour", "multiSelect": False,
                 "options": [{"label": "Red"}, {"label": "Blue"}]},
                {"id": "multiple", "header": "Multiple", "question": "Choose features", "multiSelect": True,
                 "options": [{"label": "Fast"}, {"label": "Quiet"}]},
            ]
            if status == "withdrawn":
                record = codex_form({
                    "id": 1, "method": "item/tool/requestUserInput",
                    "params": {"threadId": sid, "turnId": response_id, "itemId": tool_id,
                               "questions": native_questions},
                }, response_id)
            else:
                record = claude_form(
                    interaction_id=f"interaction-{sid}", response_id=response_id,
                    tool_use_id=tool_id, tool_name="AskUserQuestion", tool_input={"questions": native_questions},
                )
            iid = record["interaction_id"]
            await sessions.create_session({
                "session_id": sid, "user_id": scene["user_id"], "agent_id": scene["agent_id"],
                "title": f"Ended form {status}", "session_kind": "agent_chat", "state": "TERMINATED",
                "engine_kind": "codex" if status == "withdrawn" else "claude_code",
                "workspace_ref": {"kind": "agent", "agent_id": scene["agent_id"]},
            })
            lease = await facts.claim_reader(sid, owner=f"ended-form-{sid}", ttl_s=60)
            assert lease is not None
            try:
                chunks = [
                    {"type": "start", "messageId": response_id},
                    {"type": "tool-input-available", "toolCallId": tool_id,
                     "toolName": record["tool_name"], "dynamic": True, "input": record["raw_input"]},
                    {"type": "data-interaction", "data": record},
                ]
                assert await facts.record(lease, FactBatch(
                    output=[OutputPart(index, response_id, chunk) for index, chunk in enumerate(chunks, 1)],
                    facts=[ResponseStart(response_id, "engine"), InteractionOpen(response_id, iid, record)],
                ), ttl_s=60)
                if status == "answered":
                    answer = {"answers": [
                        {"question_id": record["questions"][0]["id"], "option_label": "Red"},
                        {"question_id": record["questions"][1]["id"], "option_labels": ["Fast", "Quiet"]},
                    ]}
                    validate_interaction_response(record, answer)
                    assert await facts.request_answer(sid, iid, answer=answer, answered_by=scene["user_id"])
                assert await facts.record(lease, FactBatch(
                    output=[OutputPart(4, response_id, {"type": "finish", "finishReason": "stop"})],
                    facts=[InteractionClose(iid, status), ResponseEnd(response_id, "cancelled")],
                    status="idle",
                ), ttl_s=60)
                stored = await facts.get_interaction(sid, iid)
                assert stored and stored["status"] == status
                assert await facts.open_interaction_ids(sid) == []
                result.append({"session_id": sid, "interaction_id": iid, "status": status,
                               "tool_name": record["tool_name"]})
            finally:
                await facts.release_reader(lease)
        print(json.dumps(result))
    finally:
        await get_persistence().dispose()


asyncio.run(main(json.loads(sys.argv[1])))
