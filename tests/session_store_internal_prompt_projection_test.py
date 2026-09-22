from __future__ import annotations

from copy import deepcopy
from typing import Any

from astrabox.core.service.orchestrator.engine.claude_transcript import (
    project_settled_transcript,
)
from astrabox.persistence.transcript_session_store import TranscriptSessionStore


class _Repo:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def append_entries(
        self,
        project_key: str,
        session_id: str,
        subpath: str | None,
        entries: list[dict[str, Any]],
        *,
        platform_session_id: str | None,
    ) -> int:
        _ = (project_key, session_id, subpath, platform_session_id)
        self.entries.extend(deepcopy(entries))
        return len(self.entries)

    async def load_entries(
        self,
        project_key: str,
        session_id: str,
        subpath: str | None,
        *,
        platform_session_id: str | None,
    ) -> list[dict[str, Any]]:
        _ = (project_key, session_id, subpath, platform_session_id)
        return deepcopy(self.entries)


async def test_task_and_peer_prompts_stay_opaque_store_entries_not_user_turns() -> None:
    task_prompt = {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {
            "role": "user",
            "content": "<task-notification>child finished</task-notification>",
        },
        "uuid": "task-notification-uuid",
    }
    peer_prompt = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": "peer says continue",
            "commandMode": "prompt",
            "isMeta": True,
            "origin": {
                "kind": "peer",
                "senderTaskId": "task-child",
            },
        },
    }
    repo = _Repo()
    store = TranscriptSessionStore(  # type: ignore[arg-type]
        repo,
        platform_session_id="platform-session",
    )
    key = {"project_key": "project", "session_id": "sdk-session"}

    await store.append(key, [task_prompt, peer_prompt])
    loaded = await store.load(key)
    assert loaded == [task_prompt, peer_prompt], "SessionStore keeps vendor rows opaque"

    projection = project_settled_transcript(loaded or [])
    assert projection.blocks == [] and projection.assistant_text == "", (
        "internal task/peer prompts may drive engine work but cannot project a "
        "platform user message or phantom turn"
    )
