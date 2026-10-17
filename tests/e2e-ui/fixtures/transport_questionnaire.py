"""Hold one synthetic native questionnaire on an existing fixture Session."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict

from astrabox.deploy.onebox import ensure_database_wiring

ensure_database_wiring()

from astrabox.core.service.orchestrator.engine.claude_runtime_client import interaction_record
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch, InteractionClose, InteractionOpen, OutputPart, ReaderLease, ResponseEnd,
    ResponseStart, SessionRuntimeRepository,
)


async def main(scene: dict) -> None:
    facts = SessionRuntimeRepository()
    sid = scene["session_id"]
    response_id, iid = f"transport-{sid}", f"question-{sid}"
    try:
        if "lease" in scene:
            lease = ReaderLease(**scene["lease"])
            stored = await facts.get_interaction(sid, iid)
            assert stored and stored["answer"] is not None
            assert await facts.record(lease, FactBatch(
                facts=[InteractionClose(iid, "answered"), ResponseEnd(response_id, "completed")],
                output=[OutputPart(4, response_id, {"type": "finish", "finishReason": "stop"})],
                status="idle",
            ), ttl_s=240)
            await facts.release_reader(lease)
            return
        lease = await facts.claim_reader(sid, owner=f"transport-fixture-{sid}", ttl_s=240)
        assert lease is not None, "fixture requires a Session without an engine reader"
        record = interaction_record(
            interaction_id=iid, response_id=response_id, tool_use_id=f"tool-{sid}",
            tool_name="AskUserQuestion", tool_input={"questions": [
                {"id": "single", "header": "Single", "question": "Choose one colour",
                 "multiSelect": False, "options": [{"label": "Red"}, {"label": "Blue"}]},
                {"id": "multiple", "header": "Multiple", "question": "Choose features",
                 "multiSelect": True, "options": [{"label": "Fast"}, {"label": "Quiet"}]},
                {"id": "note", "header": "Note", "question": "Add a note",
                 "multiSelect": False, "options": [{"label": "Default"}, {"label": "None"}]},
            ]},
        )
        chunks = [
            {"type": "start", "messageId": response_id},
            {"type": "tool-input-available", "toolCallId": record["tool_call_id"],
             "toolName": record["tool_name"], "dynamic": True, "input": record["raw_input"]},
            {"type": "data-interaction", "data": record},
        ]
        assert await facts.record(lease, FactBatch(
            facts=[ResponseStart(response_id, "engine"), InteractionOpen(response_id, iid, record)],
            output=[OutputPart(index, response_id, chunk) for index, chunk in enumerate(chunks, 1)],
            status="waiting",
        ), ttl_s=240)
        print(json.dumps({"interaction_id": iid, "lease": asdict(lease)}))
    finally:
        await get_persistence().dispose()


asyncio.run(main(json.loads(sys.argv[1])))
