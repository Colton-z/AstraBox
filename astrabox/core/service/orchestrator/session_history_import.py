"""Import a conversation's earlier history for reading, without running any of it.

A deployment that migrates conversations from another system brings their
messages here once. Each message becomes one ``history.imported`` event in the
session's own journal, so it is read, paged and shared exactly like the
conversation's later messages. An import starts no sandbox, runs no command and
writes no turn: it changes no lifecycle, permission, active turn or pending
interaction, and a reply whose ending the source never recorded keeps
``completion: unknown`` instead of a success or failure it did not have.

The engine's own continuation is not this: the engine resumes from its native
SessionStore, which the deployment restores separately.

Imported history precedes the conversation. A session that already holds a
message of its own refuses an import, because the journal cannot place earlier
history before events it has already ordered. Re-running an import is a no-op;
reusing a source id for other content, or adding a message before ones already
imported, is refused.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_message_view import (
    CONVERSATION_OUTPUT_EVENT_TYPES,
    HISTORY_IMPORTED_EVENT_TYPE,
)
from astrabox.core.service.orchestrator.session_public_projection import (
    PublicProjectionError,
    project_public_message_block,
)

COMPLETION_COMPLETE = "complete"
#: The source recorded no ending for this reply; it is neither a success nor a failure.
COMPLETION_UNKNOWN = "unknown"
MAX_HISTORY_IMPORT_MESSAGES = 500
MAX_SOURCE_ID_CHARS = 200

#: Content a message carries. Platform vocabulary (turn outcomes, folded work,
#: interface parts) describes a turn this platform ran, which history did not.
_BLOCK_TYPES_BY_ROLE: dict[str, frozenset[str]] = {
    "user": frozenset({"text", "image"}),
    "assistant": frozenset({"text", "image", "thinking", "tool_use", "tool_result"}),
}


@dataclass(frozen=True)
class ImportedMessage:
    source_id: str
    role: Literal["user", "assistant"]
    created_at: str
    blocks: tuple[dict[str, Any], ...]
    completion: str | None
    parent_source_id: str | None

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "role": self.role,
            "created_at": self.created_at,
            "blocks": [dict(block) for block in self.blocks],
        }
        if self.completion is not None:
            payload["completion"] = self.completion
        if self.parent_source_id is not None:
            payload["parent_source_id"] = self.parent_source_id
        payload["digest"] = _digest(payload)
        return payload


def _digest(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "digest"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invalid(index: int, message: str) -> APIError:
    return APIError(
        code="INVALID_REQUEST",
        message=f"messages[{index}]: {message}",
        status_code=400,
    )


def _timestamp(index: int, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(index, "created_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid(index, "created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _invalid(index, "created_at must carry a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _blocks(index: int, role: str, value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise _invalid(index, "blocks must be a non-empty list")
    allowed = _BLOCK_TYPES_BY_ROLE[role]
    blocks: list[dict[str, Any]] = []
    tool_use_ids: set[str] = set()
    for position, block in enumerate(value):
        if not isinstance(block, dict):
            raise _invalid(index, f"blocks[{position}] must be an object")
        block_type = str(block.get("type") or "")
        if block_type not in allowed:
            raise _invalid(
                index,
                f"blocks[{position}] type {block_type!r} is not importable for "
                f"{role} messages; use one of {sorted(allowed)}",
            )
        try:
            public = project_public_message_block(block)
        except PublicProjectionError as exc:
            raise _invalid(index, f"blocks[{position}]: {exc}") from exc
        if public != block:
            raise _invalid(
                index,
                f"blocks[{position}] has fields the history does not keep: "
                f"{sorted(set(block) - set(public or {}))}",
            )
        if block_type == "tool_use":
            tool_use_ids.add(str(block.get("id") or ""))
        if block_type == "tool_result" and str(block.get("tool_use_id") or "") not in tool_use_ids:
            # A result is shown under the call that produced it, in the reply
            # that made the call.
            raise _invalid(
                index,
                f"blocks[{position}] answers a tool_use this message has not made before it",
            )
        blocks.append(dict(block))
    return tuple(blocks)


def _source_id(index: int, value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(index, f"{field} must be a non-empty string")
    if len(value) > MAX_SOURCE_ID_CHARS or value != value.strip():
        raise _invalid(index, f"{field} must be at most {MAX_SOURCE_ID_CHARS} characters, without surrounding spaces")
    return value


def validate_history_import(messages: Any) -> list[ImportedMessage]:
    """The batch as importable messages, in the order given; refuses it whole."""
    if not isinstance(messages, list) or not messages:
        raise APIError(code="INVALID_REQUEST", message="messages must be a non-empty list", status_code=400)
    if len(messages) > MAX_HISTORY_IMPORT_MESSAGES:
        raise APIError(
            code="INVALID_REQUEST",
            message=f"an import carries at most {MAX_HISTORY_IMPORT_MESSAGES} messages",
            status_code=400,
        )
    validated: list[ImportedMessage] = []
    seen: set[str] = set()
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            raise _invalid(index, "must be an object")
        unknown = set(raw) - {"source_id", "role", "created_at", "blocks", "completion", "parent_source_id"}
        if unknown:
            raise _invalid(index, f"unknown fields {sorted(unknown)}")
        source_id = _source_id(index, raw.get("source_id"), "source_id")
        if source_id in seen:
            raise _invalid(index, f"source_id {source_id!r} appears twice in this import")
        role = raw.get("role")
        if role not in ("user", "assistant"):
            raise _invalid(index, "role must be 'user' or 'assistant'")
        completion = raw.get("completion")
        if role == "assistant" and completion not in (COMPLETION_COMPLETE, COMPLETION_UNKNOWN):
            raise _invalid(index, "an assistant message needs completion 'complete' or 'unknown'")
        if role == "user" and completion is not None:
            raise _invalid(index, "completion describes a reply; a user message has none")
        parent = raw.get("parent_source_id")
        if parent is not None:
            parent = _source_id(index, parent, "parent_source_id")
        validated.append(ImportedMessage(
            source_id=source_id,
            role=role,
            created_at=_timestamp(index, raw.get("created_at")),
            blocks=_blocks(index, role, raw.get("blocks")),
            completion=completion,
            parent_source_id=parent,
        ))
        seen.add(source_id)
    return validated


class SessionHistoryImporter:
    """Append validated history to one session's journal, idempotently."""

    def __init__(self, session_events_repo: Any) -> None:
        self._events = session_events_repo

    async def import_messages(
        self,
        session_id: str,
        messages: list[ImportedMessage],
    ) -> dict[str, int]:
        imported = await self._imported(session_id)
        stored_order = list(imported)
        new: list[ImportedMessage] = []
        matched: list[str] = []
        for message in messages:
            existing = imported.get(message.source_id)
            if existing is None:
                new.append(message)
                continue
            if new:
                raise self._conflict(
                    f"message {message.source_id!r} is already imported, but this "
                    "import places new messages before it",
                )
            if existing.get("digest") != message.payload()["digest"]:
                raise self._conflict(f"message {message.source_id!r} was imported with other content")
            matched.append(message.source_id)
        if matched != stored_order[: len(matched)]:
            raise self._conflict("this import orders already imported messages differently")
        known = set(imported)
        for message in new:
            if message.parent_source_id is not None and message.parent_source_id not in known:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=(
                        f"message {message.source_id!r} names parent "
                        f"{message.parent_source_id!r}, which no earlier message has"
                    ),
                    status_code=400,
                )
            known.add(message.source_id)
        if new and await self._conversation_started(session_id):
            raise APIError(
                code="HISTORY_IMPORT_AFTER_CONVERSATION",
                message=(
                    "this session already has messages of its own; earlier history "
                    "can only be imported before the conversation starts"
                ),
                status_code=409,
            )
        for message in new:
            payload = message.payload()
            stored, created = await self._events.try_claim_event({
                "session_id": session_id,
                "causation_id": f"history:{message.source_id}",
                "event_type": HISTORY_IMPORTED_EVENT_TYPE,
                "channel": "history",
                "turn_id": None,
                "payload": payload,
            })
            if not created and (stored.get("payload") or {}).get("digest") != payload["digest"]:
                raise self._conflict(f"message {message.source_id!r} was imported with other content")
        return {"imported": len(new), "unchanged": len(matched)}

    async def _conversation_started(self, session_id: str) -> bool:
        """Whether the session has sent or answered a message of its own.

        Every session journal starts with lifecycle commands (creation,
        permission mode); only a turn makes it a conversation.
        """
        if await self._events.list_events(
            session_id, after_seq=0, event_types=CONVERSATION_OUTPUT_EVENT_TYPES, limit=1,
        ):
            return True
        after_seq = 0
        while True:
            batch = await self._events.list_events(
                session_id, after_seq=after_seq, event_type="command.accepted", limit=500,
            )
            if any((row.get("payload") or {}).get("command_type") == "StartTurn" for row in batch):
                return True
            if len(batch) < 500:
                return False
            after_seq = max(int(row["event_seq"]) for row in batch)

    async def _imported(self, session_id: str) -> dict[str, dict[str, Any]]:
        """Already imported payloads by source id, in journal order."""
        rows: dict[str, dict[str, Any]] = {}
        after_seq = 0
        while True:
            batch = await self._events.list_events(
                session_id, after_seq=after_seq,
                event_types=(HISTORY_IMPORTED_EVENT_TYPE,), limit=500,
            )
            for row in batch:
                payload = row.get("payload") or {}
                rows[str(payload["source_id"])] = payload
            if len(batch) < 500:
                return rows
            after_seq = max(int(row["event_seq"]) for row in batch)

    @staticmethod
    def _conflict(message: str) -> APIError:
        return APIError(code="HISTORY_IMPORT_CONFLICT", message=message, status_code=409)
