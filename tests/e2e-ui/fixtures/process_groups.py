"""Persist synthetic process groups through the deployed runtime reader lease."""
from __future__ import annotations

import asyncio
import json
import sys

from astrabox.deploy.onebox import ensure_database_wiring

ensure_database_wiring()

from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch, OutputPart, ResponseEnd, ResponseStart, SessionRuntimeRepository,
)


async def main(scene: dict) -> None:
    sessions, facts = SessionRepository(), SessionRuntimeRepository()
    records = []
    try:
        for kind, sid in scene["sessions"].items():
            rid = f"process-{sid}"
            chunks = [{"type": "start", "messageId": rid}]

            def text(name: str, body: str, part_type: str = "text") -> None:
                chunks.extend([
                    {"type": f"{part_type}-start", "id": name},
                    {"type": f"{part_type}-delta", "id": name, "delta": body},
                    {"type": f"{part_type}-end", "id": name},
                ])

            def tool(name: str, tool_name: str, failed: bool = False) -> None:
                chunks.append({"type": "tool-input-available", "toolCallId": name,
                               "toolName": tool_name, "dynamic": True,
                               "input": {"description": name, "command": name}})
                chunks.append({"type": "tool-output-error", "toolCallId": name, "errorText": "FAILED_OUTPUT"}
                              if failed else {"type": "tool-output-available", "toolCallId": name,
                                              "output": name.upper() + "_OUTPUT"})

            text("begin", "Beginning inspection")
            text("thinking", "PROCESS_THINKING", "reasoning")
            tool("first", "Read")
            tool("second", "Bash")
            text("stage", "Intermediate conclusion")
            tool("third", "Read")
            text("final", "Final conclusion")
            if kind == "ordinary":
                tool("failed", "Bash", failed=True)
            chunks.append({"type": "finish", "finishReason": "stop"})
            await sessions.create_session({
                "session_id": sid, "user_id": scene["user_id"], "agent_id": scene["agent_id"],
                "title": f"Synthetic process {kind}", "session_kind": "agent_chat",
                "state": "TERMINATED", "engine_kind": "claude_code",
                "workspace_ref": {"kind": "agent", "agent_id": scene["agent_id"]},
            })
            lease = await facts.claim_reader(sid, owner=f"process-fixture-{sid}", ttl_s=60)
            assert lease is not None
            try:
                assert await facts.record(lease, FactBatch(
                    output=[OutputPart(index, rid, chunk) for index, chunk in enumerate(chunks, 1)],
                    facts=[ResponseStart(rid, "engine"), ResponseEnd(rid, "completed", native_reason="end_turn")],
                    status="idle",
                ), ttl_s=60)
            finally:
                await facts.release_reader(lease)
            if kind == "summary":
                summaries = ProcessSummaryRepository()
                token = await summaries.claim(sid, rid, through_seq=len(chunks), response_completed=True)
                assert token is not None
                assert await summaries.finish(sid, rid, claim_token=token, status="completed",
                                              summary="Synthetic inspection process", error=None)
            records.append({"session_id": sid, "response_id": rid, "kind": kind, "chunks": chunks})
        print(json.dumps(records))
    finally:
        await get_persistence().dispose()


asyncio.run(main(json.loads(sys.argv[1])))
