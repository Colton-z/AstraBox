"""Project settled background tasks from durable engine evidence.

A background task can outlive its sandbox, so materialization cannot depend on
a live endpoint. The resident SDK stream and its SessionStore are complementary
durable sources: the former covers terminals that never append a transcript
line, while the latter survives a server restart that loses the live reader.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401  (self-registers)
from astrabox.core.service.orchestrator.engine.child_runs import public_child_run_id
from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    UnknownWireEvent,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    _BACKGROUND_CONTINUATION_CONCURRENCY,
    BackgroundContinuationMixin,
)


class _FakeJournalRepo:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._events = [
            {
                "event_seq": index,
                "channel": "conversation",
                "event_type": "engine.message",
                "payload": {"engine_kind": "claude_code", "message": entry},
            }
            for index, entry in enumerate(entries, start=1)
        ]
        self.calls: list[dict[str, Any]] = []

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        channel: str | None = None,
        event_type: str | None = None,
        limit: int = 500,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "session_id": session_id,
                "after_seq": after_seq,
                "channel": channel,
                "event_type": event_type,
                "limit": limit,
            }
        )
        return [dict(event) for event in self._events if int(event["event_seq"]) > after_seq][
            :limit
        ]


class _FakeTranscriptRepo:
    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        subpath_entries: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._entries = list(entries)
        self._subpath_entries = dict(subpath_entries or {})
        self.scope_calls: list[str] = []
        self.subpath_calls: list[tuple[str, str | None]] = []

    async def list_scopes_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        self.scope_calls.append(platform_session_id)
        return [
            {
                "project_key": "project",
                "session_id": "sdk-session",
                "subpath": subpath,
            }
            for subpath in [None, *self._subpath_entries]
        ]

    async def load_subpath_entries_by_platform_session(
        self,
        platform_session_id: str,
        *,
        subpath: str | None,
    ) -> list[dict[str, Any]]:
        self.subpath_calls.append((platform_session_id, subpath))
        if subpath is None:
            return list(self._entries)
        return list(self._subpath_entries.get(subpath, []))


class _Harness(BackgroundContinuationMixin):
    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        transcript_entries: list[dict[str, Any]] | None = None,
        subpath_entries: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._session_events_repo = _FakeJournalRepo(entries)
        self._transcript_entries_repo = _FakeTranscriptRepo(
            transcript_entries or [],
            subpath_entries=subpath_entries,
        )


class _BatchHarness(BackgroundContinuationMixin):
    def __init__(self, event_count: int, *, failed_event_seq: int | None = None) -> None:
        self._opened_events = [
            {
                "session_id": f"sess-{event_seq}",
                "turn_id": f"turn-{event_seq}",
                "event_seq": event_seq,
            }
            for event_seq in range(1, event_count + 1)
        ]
        self._failed_event_seq = failed_event_seq
        self.active = 0
        self.max_active = 0
        self.started: list[int] = []
        self.capacity_reached = asyncio.Event()
        self.release = asyncio.Event()

    async def _list_background_task_opened_events(
        self,
        *,
        limit: int = 50,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        return [dict(event) for event in self._opened_events[skip:skip + limit]]

    async def _get_background_materialized_event(
        self,
        opened_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        return None

    async def _materialize_background_continuation_event(
        self,
        opened_event: dict[str, Any],
    ) -> bool:
        event_seq = int(opened_event["event_seq"])
        self.started.append(event_seq)
        if event_seq == self._failed_event_seq:
            raise RuntimeError("materialization failed")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == _BACKGROUND_CONTINUATION_CONCURRENCY:
            self.capacity_reached.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return True


class _ScanHarness(BackgroundContinuationMixin):
    """A journal where the newest manifests are settled and an old one is not."""

    def __init__(self, *, newest_seq: int, open_seqs: set[int]) -> None:
        # Newest first, as the journal query orders them.
        self._opened_events = [
            {"session_id": "sess", "turn_id": f"turn-{seq}", "event_seq": seq}
            for seq in range(newest_seq, 0, -1)
        ]
        self._open_seqs = set(open_seqs)
        self.materialized: list[int] = []
        self.pages: list[tuple[int, int]] = []

    async def _list_background_task_opened_events(
        self,
        *,
        limit: int = 50,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        self.pages.append((limit, skip))
        return [dict(event) for event in self._opened_events[skip:skip + limit]]

    async def _get_background_materialized_event(
        self,
        opened_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        seq = int(opened_event["event_seq"])
        return None if seq in self._open_seqs else {"event_seq": seq + 1000}

    async def _materialize_background_continuation_event(
        self,
        opened_event: dict[str, Any],
    ) -> bool:
        self.materialized.append(int(opened_event["event_seq"]))
        return True


class BackgroundContinuationScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_open_manifest_behind_fifty_settled_ones_is_still_materialized(self) -> None:
        """A background result is owed to its conversation however many
        newer manifests the deployment opened and settled since."""

        harness = _ScanHarness(newest_seq=60, open_seqs={3})
        assert await harness._materialize_background_continuations_once(limit=50) == 1
        assert harness.materialized == [3]
        assert harness.pages == [(50, 0), (50, 50)]

    async def test_the_scan_stops_once_it_holds_a_full_page_of_open_manifests(self) -> None:
        harness = _ScanHarness(newest_seq=120, open_seqs=set(range(1, 121)))
        assert await harness._materialize_background_continuations_once(limit=50) == 50
        assert harness.materialized == list(range(120, 70, -1))
        assert harness.pages == [(50, 0)]


class BackgroundContinuationBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_manifests_are_attempted_in_bounded_parallel(self) -> None:
        harness = _BatchHarness(_BACKGROUND_CONTINUATION_CONCURRENCY + 1)

        batch = asyncio.create_task(
            harness._materialize_background_continuations_once()
        )
        await asyncio.wait_for(harness.capacity_reached.wait(), timeout=1)

        assert harness.max_active == _BACKGROUND_CONTINUATION_CONCURRENCY
        assert len(harness.started) == _BACKGROUND_CONTINUATION_CONCURRENCY

        harness.release.set()
        assert await batch == _BACKGROUND_CONTINUATION_CONCURRENCY + 1
        assert harness.max_active == _BACKGROUND_CONTINUATION_CONCURRENCY

    async def test_one_failed_manifest_does_not_cancel_its_siblings(self) -> None:
        harness = _BatchHarness(3, failed_event_seq=2)
        harness.release.set()

        assert await harness._materialize_background_continuations_once() == 2
        assert harness.started == [1, 2, 3]


def _notification(tool_use_id: str | None, task_id: str) -> dict[str, Any]:
    return {
        "__sdk_type": "TaskNotificationMessage",
        "subtype": "task_notification",
        "data": {
            "task_id": task_id,
            "status": "completed",
            "output_file": "/tmp/task.output",
            "summary": "THE ANSWER IS 42",
            "uuid": "u-notify",
            "session_id": "sdk-session",
            "tool_use_id": tool_use_id,
        },
        "task_id": task_id,
        "status": "completed",
        "output_file": "/tmp/task.output",
        "summary": "THE ANSWER IS 42",
        "uuid": "u-notify",
        "session_id": "sdk-session",
        "tool_use_id": tool_use_id,
        "usage": None,
    }


def _updated(task_id: str) -> dict[str, Any]:
    return {
        "__sdk_type": "TaskUpdatedMessage",
        "subtype": "task_updated",
        "data": {
            "task_id": task_id,
            "patch": {"status": "completed", "end_time": 1786320313958},
            "uuid": "u-updated",
            "session_id": "sdk-session",
        },
        "task_id": task_id,
        "patch": {"status": "completed", "end_time": 1786320313958},
        "status": "completed",
        "uuid": "u-updated",
        "session_id": "sdk-session",
    }


def _started(tool_use_id: str, task_id: str) -> dict[str, Any]:
    return {
        "__sdk_type": "TaskStartedMessage",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "description": "compute the marker",
        "uuid": "u-started",
        "session_id": "sdk-session",
    }


def _transcript_notification(tool_use_id: str, task_id: str) -> dict[str, Any]:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": (
            "<task-notification>"
            f"<task-id>{task_id}</task-id>"
            f"<tool-use-id>{tool_use_id}</tool-use-id>"
            "<status>completed</status>"
            "<summary>Agent finished</summary>"
            "<result>RECOVERED ANSWER 42</result>"
            "</task-notification>"
        ),
    }


def _child_transcript() -> list[dict[str, Any]]:
    return [
        {
            "type": "agent_metadata",
            "agentType": "general-purpose",
            "description": "compute the marker",
            "toolUseId": "call_00_a",
            "spawnDepth": 1,
        },
        {
            "type": "assistant",
            "uuid": "child-assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "child-bash",
                        "name": "Bash",
                        "input": {"command": "printf RECOVERED_CHILD_42"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "child-user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "child-bash",
                        "content": "RECOVERED_CHILD_42",
                    }
                ],
            },
        },
    ]


#: The journal row a manifest is materialized from. Only the fields the gate
#: log reads are needed here; the projection itself never looks at it.
_OPENED_EVENT = {"session_id": "sess-1", "turn_id": "turn-1", "event_seq": 7}

_MANIFEST = {
    "transcript_refs": ["agent-session-aa"],
    "engine_refs": ["agent-session-aa"],
    "transcript_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
    "control_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
    "activation_to_engine_ref": {"call_00_a": "agent-session-aa"},
}


class CollectProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        harness = _Harness(entries)
        return await harness._collect_background_continuation_projection(
            session_id="sess-1",
            parent_turn_id="turn-1",
            manifest_payload=dict(_MANIFEST),
            adapter=get_engine_adapter("claude_code"),
            opened_event=dict(_OPENED_EVENT),
        )

    async def test_a_settled_task_projects_lifecycle_and_result(self) -> None:
        projection = await self._collect(
            [_notification("call_00_a", "agent-session-aa")]
        )

        assert projection is not None
        assert projection["remaining_transcript_refs"] == []
        assert projection["remaining_engine_refs"] == []
        assert "assistant_text" not in projection

        lifecycle = next(
            block
            for block in projection["blocks"]
            if block.get("data", {}).get("kind") == "lifecycle"
        )
        assert lifecycle["data"]["event"] == "closed"
        assert lifecycle["data"]["engineEvent"] == "task_notification"
        assert lifecycle["data"]["engineStatus"] == "completed"
        assert lifecycle["data"]["engineRef"] == "agent-session-aa"
        assert lifecycle["data"]["controlRef"] == "agent-session-aa"
        assert "childRunId" not in lifecycle["data"]
        assert lifecycle["data"]["summary"] == "THE ANSWER IS 42"
        assert not any(block.get("data", {}).get("kind") == "message" for block in projection["blocks"])
        public_id = public_child_run_id(
            session_id="sess-1",
            engine_kind="claude_code",
            engine_ref="agent-session-aa",
        )
        assert public_id in lifecycle["id"]
        assert "call_00_a" not in lifecycle["id"]

    async def test_an_unsettled_task_projects_nothing(self) -> None:
        """Full-drain only: the materialized event is claimed exactly once per
        opened manifest, so a partial answer now would permanently swallow the
        completions that arrive later."""
        assert await self._collect([]) is None
        assert await self._collect([_notification("call_other", "task_other")]) is None

    async def test_notification_enriches_the_preceding_empty_task_update(self) -> None:
        """The notification carries the marker missing from its preceding update."""
        projection = await self._collect(
            [
                _started("call_00_a", "agent-session-aa"),
                _updated("agent-session-aa"),
                _notification(None, "agent-session-aa"),
            ]
        )

        assert projection is not None
        lifecycle = next(block for block in projection["blocks"] if block.get("data", {}).get("kind") == "lifecycle")
        assert lifecycle["data"]["summary"] == "THE ANSWER IS 42"

    async def test_transcript_recovers_a_terminal_lost_with_the_live_reader(self) -> None:
        """After restart the journal can stop before terminal while SessionStore continues."""
        child_path = "subagents/agent-agent-session-aa"
        harness = _Harness(
            [],
            transcript_entries=[
                _transcript_notification("call_00_a", "agent-session-aa")
            ],
            subpath_entries={child_path: _child_transcript()},
        )

        projection = await harness._collect_background_continuation_projection(
            session_id="sess-1",
            parent_turn_id="turn-1",
            manifest_payload=dict(_MANIFEST),
            adapter=get_engine_adapter("claude_code"),
            opened_event=dict(_OPENED_EVENT),
        )

        assert projection is not None
        assert any(
            block.get("data", {}).get("summary") == "Agent finished"
            for block in projection["blocks"]
        )
        assert "RECOVERED ANSWER 42" not in str(projection["blocks"])
        replayed = {
            block["id"]: block
            for block in projection["blocks"]
            if block.get("id") in {"subagent:msg:child-assistant", "subagent:msg:child-user"}
        }
        assert replayed["subagent:msg:child-assistant"]["data"]["content"][0] == {
            "type": "tool_use",
            "id": "child-bash",
            "name": "Bash",
            "input": {"command": "printf RECOVERED_CHILD_42"},
        }
        assert replayed["subagent:msg:child-user"]["data"]["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "child-bash",
            "content": "RECOVERED_CHILD_42",
        }
        assert harness._transcript_entries_repo.subpath_calls == [
            ("sess-1", None),
            ("sess-1", child_path),
        ]

    async def test_nested_agent_metadata_projects_the_complete_child_run_tree(self) -> None:
        root_path = "subagents/agent-agent-session-aa"
        nested_path = "subagents/agent-nested-agent"
        nested_tool_id = "call_00_nested"
        harness = _Harness(
            [_notification("call_00_a", "agent-session-aa")],
            subpath_entries={
                root_path: [
                    *_child_transcript(),
                    {
                        "type": "assistant",
                        "uuid": "root-spawns-nested",
                        "message": {
                            "role": "assistant",
                            "stop_reason": "tool_use",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": nested_tool_id,
                                    "name": "Agent",
                                    "input": {
                                        "description": "nested leaf",
                                        "subagent_type": "general-purpose",
                                        "run_in_background": False,
                                    },
                                }
                            ],
                        },
                    },
                ],
                nested_path: [
                    {
                        "type": "agent_metadata",
                        "agentType": "general-purpose",
                        "description": "nested leaf",
                        "toolUseId": nested_tool_id,
                        "parentAgentId": "agent-session-aa",
                        "spawnDepth": 2,
                    },
                    {
                        "type": "assistant",
                        "uuid": "nested-bash",
                        "message": {
                            "role": "assistant",
                            "stop_reason": "tool_use",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "nested-bash-call",
                                    "name": "Bash",
                                    "input": {"command": "printf NESTED_LEAF_42"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "nested-bash-result",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "nested-bash-call",
                                    "content": "NESTED_LEAF_42",
                                    "is_error": False,
                                }
                            ],
                        },
                    },
                    {
                        "type": "assistant",
                        "uuid": "nested-finished",
                        "message": {
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "NESTED_LEAF_42"}],
                        },
                    },
                ],
            },
        )

        projection = await harness._collect_background_continuation_projection(
            session_id="sess-1",
            parent_turn_id="turn-1",
            manifest_payload=dict(_MANIFEST),
            adapter=get_engine_adapter("claude_code"),
            opened_event=dict(_OPENED_EVENT),
        )

        assert projection is not None
        nested_blocks = [
            block
            for block in projection["blocks"]
            if block.get("data", {}).get("engineRef") == "nested-agent"
        ]
        assert nested_blocks
        assert all(block["data"]["parentEngineRef"] == "agent-session-aa" for block in nested_blocks)
        lifecycle = next(block for block in nested_blocks if block["data"]["kind"] == "lifecycle")
        assert lifecycle["data"]["event"] == "closed"
        assert lifecycle["data"]["engineEvent"] == "session_store.stop_reason"
        assert lifecycle["data"]["engineReason"] == "end_turn"
        assert lifecycle["data"]["description"] == "nested leaf"
        assert "NESTED_LEAF_42" in str(nested_blocks)

    async def test_both_durable_sources_are_consulted_without_a_sandbox(self) -> None:
        harness = _Harness([_notification("call_00_a", "agent-session-aa")])
        projection = await harness._collect_background_continuation_projection(
            session_id="sess-1",
            parent_turn_id="turn-1",
            manifest_payload=dict(_MANIFEST),
            adapter=get_engine_adapter("claude_code"),
            opened_event=dict(_OPENED_EVENT),
        )
        assert projection is not None
        assert harness._session_events_repo.calls == [
            {
                "session_id": "sess-1",
                "after_seq": 0,
                "channel": "conversation",
                "event_type": "engine.message",
                "limit": 500,
            }
        ]
        assert harness._transcript_entries_repo.scope_calls == ["sess-1"]
        assert harness._transcript_entries_repo.subpath_calls == [("sess-1", None)]


class SubagentLifecycleProjectionTests(unittest.TestCase):
    """The live-turn projection reads BOTH spellings of a lifecycle envelope.

    A runner-serialized TaskStartedMessage carries `__sdk_type` and no
    `type: "system"` key. Both that shape and the CLI spelling must project a
    `started` lifecycle block so the Agents panel can show a complete task
    lifecycle.
    """

    def test_a_runner_serialized_task_started_projects_an_opened_fact(self) -> None:
        (frame,) = list(
            translate_claude_sdk_message(
            {
                "__sdk_type": "TaskStartedMessage",
                "subtype": "task_started",
                "data": {},
                "task_id": "task_aa",
                "description": "run the long thing",
                "usage": {"total_tokens": 0, "tool_uses": 0, "duration_ms": 0},
                "uuid": "u-started",
                "session_id": "s-1",
                "tool_use_id": "call_00_a",
            },
                envelope_seq=1,
                cursor=ClaudeStreamCursor(),
            )
        )

        assert frame["data"]["event"] == "opened"
        assert frame["data"]["engineEvent"] == "task_started"
        assert frame["data"]["engineRef"] == "task_aa"
        assert frame["data"]["controlRef"] == "task_aa"
        assert frame["data"]["operations"] == ["stop"]

    def test_a_task_updated_killed_remains_killed(self) -> None:
        (frame,) = list(
            translate_claude_sdk_message(
            {
                "__sdk_type": "TaskUpdatedMessage",
                "subtype": "task_updated",
                "data": {},
                "task_id": "task_aa",
                "patch": {"status": "killed"},
                "tool_use_id": "call_00_a",
                "uuid": "u-killed",
            },
                envelope_seq=1,
                cursor=ClaudeStreamCursor(),
            )
        )

        assert frame["data"]["event"] == "closed"
        assert frame["data"]["engineEvent"] == "task_updated"
        assert frame["data"]["engineStatus"] == "killed"
        assert frame["data"]["operations"] == []

    def test_task_updated_nonterminal_statuses_cross_the_seam_verbatim(self) -> None:
        for vendor_status in ("pending", "running", "paused"):
            with self.subTest(vendor_status=vendor_status):
                (frame,) = list(
                    translate_claude_sdk_message(
                    {
                        "__sdk_type": "TaskUpdatedMessage",
                        "subtype": "task_updated",
                        "data": {},
                        "task_id": "task_aa",
                        "patch": {"status": vendor_status},
                        "uuid": f"u-{vendor_status}",
                        "session_id": "s-1",
                        "tool_use_id": "call_00_a",
                    },
                        envelope_seq=1,
                        cursor=ClaudeStreamCursor(),
                    )
                )

                assert frame["data"]["event"] == "updated"
                assert frame["data"]["engineStatus"] == vendor_status
                assert "phase" not in frame["data"]

    def test_task_updated_without_status_updates_the_known_task(self) -> None:
        cursor = ClaudeStreamCursor()
        list(
            translate_claude_sdk_message(
                {
                    "__sdk_type": "TaskStartedMessage",
                    "subtype": "task_started",
                    "data": {},
                    "task_id": "task_aa",
                    "description": "run the long thing",
                    "uuid": "u-started",
                    "session_id": "s-1",
                    "tool_use_id": "call_00_a",
                    "task_type": "local_agent",
                },
                envelope_seq=1,
                cursor=cursor,
            )
        )

        (frame,) = list(
            translate_claude_sdk_message(
                {
                    "__sdk_type": "TaskUpdatedMessage",
                    "subtype": "task_updated",
                    "data": {},
                    "task_id": "task_aa",
                    "patch": {"is_backgrounded": True},
                    "status": None,
                    "uuid": "u-backgrounded",
                    "session_id": "s-1",
                },
                envelope_seq=2,
                cursor=cursor,
            )
        )

        assert frame["data"]["event"] == "updated"
        assert frame["data"]["engineEvent"] == "task_updated"
        assert frame["data"]["engineRef"] == "task_aa"
        assert frame["data"]["controlRef"] == "task_aa"
        assert frame["data"]["operations"] == ["stop"]
        assert "engineStatus" not in frame["data"]

    def test_task_notification_without_status_is_refused(self) -> None:
        with self.assertRaisesRegex(
            UnknownWireEvent,
            "task_notification lacks the SDK task status",
        ):
            list(
                translate_claude_sdk_message(
                    {
                        "__sdk_type": "TaskNotificationMessage",
                        "subtype": "task_notification",
                        "data": {},
                        "task_id": "task_aa",
                        "status": None,
                        "tool_use_id": "call_00_a",
                        "uuid": "u-terminal",
                    },
                    envelope_seq=1,
                    cursor=ClaudeStreamCursor(),
                )
            )


if __name__ == "__main__":
    unittest.main()


class TurnTailSliceTests(unittest.TestCase):
    """`slice_turn_tail_entries` — the CLI's own turn boundary, not a stamp."""

    def test_the_last_matching_prompt_starts_the_slice(self) -> None:
        from astrabox.core.service.orchestrator.engine.claude_transcript import (
            slice_turn_tail_entries,
        )

        earlier_turn = [
            {"type": "user", "message": {"role": "user", "content": "continue"}},
            {"type": "assistant", "message": {"role": "assistant", "content": []}},
        ]
        this_turn = [
            {"type": "user", "message": {"role": "user", "content": "continue"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "tail"}]},
            },
        ]
        sliced = slice_turn_tail_entries(earlier_turn + this_turn, prompt_text="continue")

        assert sliced == this_turn, (
            "an identical earlier prompt must not capture the slice — recovery "
            "is only ever asked about the session's newest turn"
        )

    def test_sidechain_and_notification_user_entries_never_anchor(self) -> None:
        from astrabox.core.service.orchestrator.engine.claude_transcript import (
            slice_turn_tail_entries,
        )

        items = [
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "do the thing"},
            },
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "message": {"role": "user", "content": "do the thing"},
            },
        ]
        sliced = slice_turn_tail_entries(items, prompt_text="do the thing")

        assert sliced and sliced[0] is not items[1] and sliced[0] == items[0]

    def test_no_match_means_the_mirror_does_not_cover_the_turn(self) -> None:
        from astrabox.core.service.orchestrator.engine.claude_transcript import (
            slice_turn_tail_entries,
        )

        assert (
            slice_turn_tail_entries(
                [{"type": "assistant", "message": {"role": "assistant", "content": []}}],
                prompt_text="anything",
            )
            == []
        )
        assert slice_turn_tail_entries([], prompt_text="") == []
