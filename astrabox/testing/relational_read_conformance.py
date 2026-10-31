"""Read-path contracts shared by relational backend test bindings."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from sqlalchemy import event, insert, select, update

from astrabox.core.service.orchestrator.engine import claude_code  # noqa: F401 - register the native adapter
from astrabox.core.service.orchestrator.engine.child_runs import canonical_child_run_data, public_child_run_id
from astrabox.core.service.orchestrator.session_runtime.child_runs import child_run_messages, list_child_runs
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.transcript_entry_repository import TranscriptEntryRepository
from astrabox.persistence.sql import fetch_all
from astrabox.persistence.sql.engine import get_engine
from astrabox.persistence.sql.models.agents import AgentRuntime
from astrabox.persistence.sql.models.sessions import SessionOutput, TranscriptChildRunIndex


async def verify_existing_runtime_ensure_is_read_only() -> None:
    """Existing ledgers remain readable during a writer; missing targets are created once."""
    engine = get_engine()
    repo = AgentRepository()
    await RuntimeProfileRepository().put(
        "ensure-read", {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="contract",
    )
    for n in range(24):
        await repo.create_agent({
            "agent_id": f"ensure-{n}", "name": f"ensure-{n}", "model": "model",
            "runtime_profile_name": "ensure-read", "created_by": "owner", "state": "ACTIVE",
        })
        await repo.ensure_runtime(f"ensure-{n}")
    statements: list[str] = []

    def trace(*args: Any) -> None:
        statements.append(str(args[2]))

    async with engine.begin() as writer:
        await writer.execute(update(AgentRuntime).where(
            AgentRuntime.agent_id == "ensure-0", AgentRuntime.environment_name == repo.environment_name,
        ).values(runtime_generation="writer-held"))
        event.listen(engine.sync_engine, "before_cursor_execute", trace)
        started = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.gather(*(repo.ensure_runtime(f"ensure-{n}") for n in range(24))), 3)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", trace)
        elapsed = time.monotonic() - started
        assert len(statements) == 24
        assert all(sql.lstrip().upper().startswith("SELECT ") and "FOR UPDATE" not in sql.upper()
                   and "FOR SHARE" not in sql.upper() for sql in statements), statements
    other = AgentRepository(environment_name="ensure-other-environment")
    await asyncio.gather(*(other.ensure_runtime("ensure-0") for _ in range(24)))
    await other.ensure_runtime("missing-agent")
    rows = await fetch_all(select(AgentRuntime.agent_id, AgentRuntime.environment_name))
    assert len(rows) == 25
    assert sum(row["environment_name"] == other.environment_name for row in rows) == 1
    print(json.dumps({"existing_ensures": 24, "sql_selects": len(statements), "writes": 0,
                      "writer_held_seconds": elapsed, "concurrent_missing_ensures": 24,
                      "exact_target_rows": len(rows)}))


async def verify_child_listing_indexes_native_relationships() -> None:
    """Large native bodies do not repeat on list; lineage and full child history survive."""
    engine = get_engine()
    sid = "child-index-contract"
    await SessionRepository().create_session({
        "session_id": sid, "user_id": "owner", "session_kind": "agent_chat", "state": "READY",
    })
    await SessionRepository().create_session({
        "session_id": "other-owner", "user_id": "other", "session_kind": "agent_chat", "state": "READY",
    })
    native = TranscriptEntryRepository()
    padding = "unrelated native conversation " + "x" * 8192
    main = [{"type": "user", "message": {"role": "user", "content": padding}} for _ in range(4096)]
    main.insert(2048, {"type": "user", "toolUseResult": {
        "status": "async_launched", "runId": "workflow-run", "taskId": "workflow-task",
    }})
    for start in range(0, len(main), 128):
        await native.append_entries("project", "native", None, main[start:start + 128],
                                    append_id=f"main-{start}", platform_session_id=sid)
    bodies: dict[str, list[dict[str, Any]]] = {}
    for agent, parent in (("first", ""), ("nested", "first")):
        body: list[dict[str, Any]] = [
            {"type": "agent_metadata", "parentAgentId": parent, "description": f"Workflow {agent}", "agentType": "general"},
            *({"type": "user", "message": {"role": "user", "content": padding}} for _ in range(1024)),
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"native text {agent}"},
                {"type": "tool_use", "id": f"tool-{agent}", "name": "Read", "input": {"file_path": f"/{agent}.txt"}},
            ], "stop_reason": "tool_use"}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tool-{agent}", "content": f"native result {agent}"},
            ]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"native answer {agent}"},
            ], "stop_reason": "end_turn"}},
        ]
        bodies[agent] = body
        for start in range(0, len(body), 128):
            await native.append_entries(
                "project", "native", f"subagents/workflows/workflow-run/agent-{agent}", body[start:start + 128],
                append_id=f"{agent}-{start}", platform_session_id=sid,
            )
    async with engine.begin() as conn:
        for seq, ref in enumerate(("workflow-task", "ordinary-agent"), 1):
            data = canonical_child_run_data({
                "kind": "lifecycle", "engineRef": ref, "event": "opened", "engineEvent": "task_started", "operations": [],
            }, engine_kind="claude_code")
            await conn.execute(insert(SessionOutput).values(
                session_id=sid, seq=seq, part={"type": "data-subagent", "id": f"child-{seq}", "data": data},
            ))
    reads = SessionReadRepository()
    native_queries: list[str] = []
    gaps: list[float] = []
    running = True

    def trace(*args: Any) -> None:
        sql = str(args[2])
        if "transcript_entries" in sql and sql.lstrip().upper().startswith("SELECT "):
            native_queries.append(sql)

    async def heartbeat() -> None:
        previous = time.monotonic()
        while running:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    ticker = asyncio.create_task(heartbeat())
    event.listen(engine.sync_engine, "before_cursor_execute", trace)
    try:
        cold = await list_child_runs(reads, sid)
        cold_queries = len(native_queries)
        assert cold_queries > 1 and all("LIMIT " in sql for sql in native_queries)
        native_queries.clear()
        assert await list_child_runs(reads, sid) == cold
        assert native_queries == [], "warm listing read native conversation bodies"
        assert await list_child_runs(reads, "other-owner") == []
        expected = [public_child_run_id(session_id=sid, engine_kind="claude_code", engine_ref=ref)
                    for ref in ("workflow-task", "first", "nested", "ordinary-agent")]
        assert [child["child_run_id"] for child in cold] == expected
        assert cold[1]["parent_child_run_id"] == expected[0] and cold[2]["parent_child_run_id"] == expected[1]
        assert cold[1]["closed"] and cold[2]["closed"] and cold[1]["engine_reason"] == "end_turn"
        appended = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "new native tool turn"},
        ], "stop_reason": "tool_use"}}
        await native.append_entries("project", "native", "subagents/workflows/workflow-run/agent-first", [appended],
                                    append_id="new-turn", platform_session_id=sid)
        native_queries.clear()
        advanced = await list_child_runs(reads, sid)
        incremental_queries = len(native_queries)
        assert incremental_queries == 1 and "LIMIT " in native_queries[0]
        assert not advanced[1]["closed"] and advanced[1]["engine_reason"] == "tool_use"
    finally:
        running = False
        await ticker
        event.remove(engine.sync_engine, "before_cursor_execute", trace)
    assert gaps and max(gaps) < 0.2, f"child listing stalled the event loop: {max(gaps):.3f}s"
    for index, agent in ((1, "first"), (2, "nested")):
        history = await child_run_messages(reads, sid, expected[index])
        rendered = json.dumps(history)
        assert all(value in rendered for value in (
            f"native text {agent}", f"/{agent}.txt", f"native result {agent}", f"native answer {agent}",
        )), rendered[-4000:]
        assert await native.load_subpath_entries_by_platform_session(
            sid, subpath=f"subagents/workflows/workflow-run/agent-{agent}",
        ) == bodies[agent] + ([appended] if agent == "first" else [])
    indexes = await fetch_all(select(TranscriptChildRunIndex))
    assert len(indexes) == 3 and sum(len(row["entries"]) for row in indexes) == 5
    print(json.dumps({"native_entries": len(main) + sum(map(len, bodies.values())) + 1,
                      "native_body_bytes": len(json.dumps(main)) + sum(len(json.dumps(b)) for b in bodies.values()),
                      "children": len(cold), "cold_bounded_pages": cold_queries, "warm_native_reads": 0,
                      "incremental_pages": incremental_queries, "max_loop_gap_seconds": max(gaps),
                      "complete_native_child_histories": 2, "projected_entries": 5}))
