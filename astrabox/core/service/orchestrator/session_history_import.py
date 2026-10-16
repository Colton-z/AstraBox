"""Import a conversation's earlier history for reading, without running any of it.

A deployment that migrates conversations from another system brings their
messages here once. A user message becomes an input no runtime is handed, and
a reply becomes a response that has already ended, with its content as the
browser parts the page folds; both keep the source's timestamp, so they read,
page and share in the conversation's order like its later messages. An import
starts no sandbox, runs no command and changes no session state, and a reply
whose ending the source never recorded keeps ``completion: unknown`` instead
of a success or failure it did not have.

The engine's own continuation is not this: the engine resumes from its native
SessionStore, which the deployment restores separately.

Imported history precedes the conversation. A session that already holds a
message of its own refuses an import, because history cannot be placed before
messages the conversation has ordered. Re-running an import is a no-op; reusing
a source id for other content, or adding a message before ones already
imported, is refused.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from astrabox.common.utils.errors import APIError
from astrabox.persistence.repository.session_runtime_repository import (
    HistoryEntry,
    SessionRuntimeRepository,
)
from astrabox.persistence.sql import UniqueViolation

COMPLETION_COMPLETE = "complete"
#: The source recorded no ending for this reply; it is neither a success nor a failure.
COMPLETION_UNKNOWN = "unknown"
MAX_HISTORY_IMPORT_MESSAGES = 500
MAX_SOURCE_ID_CHARS = 200

#: Content a message carries, and the fields each block keeps. Platform
#: vocabulary (outcomes, folded work, interface parts) describes a response
#: this platform ran, which history did not.
_BLOCK_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"type", "text"}),
    "image": frozenset({"type", "source"}),
    "thinking": frozenset({"type", "thinking"}),
    "tool_use": frozenset({"type", "id", "name", "input"}),
    "tool_result": frozenset({"type", "tool_use_id", "content", "is_error", "tool_result_state"}),
}
_IMAGE_SOURCE_FIELDS = frozenset({"type", "media_type", "data"})
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


def _kept(index: int, position: int, block: dict[str, Any]) -> dict[str, Any]:
    """The block as history keeps it; refuses fields it would drop."""
    fields = _BLOCK_FIELDS[str(block["type"])]
    kept = {key: deepcopy(block[key]) for key in fields if key in block}
    if block["type"] == "image":
        source = block.get("source")
        if not isinstance(source, dict):
            raise _invalid(index, f"blocks[{position}]: image source must be an object")
        kept["source"] = {key: deepcopy(source[key]) for key in _IMAGE_SOURCE_FIELDS if key in source}
    if kept != block:
        dropped = sorted(set(block) - fields)
        if block["type"] == "image" and not dropped:
            dropped = [f"source.{key}" for key in sorted(set(block["source"]) - _IMAGE_SOURCE_FIELDS)]
        raise _invalid(index, f"blocks[{position}] has fields the history does not keep: {dropped}")
    return kept


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
        kept = _kept(index, position, block)
        if block_type == "tool_use":
            tool_use_ids.add(str(block.get("id") or ""))
        if block_type == "tool_result" and str(block.get("tool_use_id") or "") not in tool_use_ids:
            # A result is shown under the call that produced it, in the reply
            # that made the call.
            raise _invalid(
                index,
                f"blocks[{position}] answers a tool_use this message has not made before it",
            )
        blocks.append(kept)
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


def reply_parts(message: ImportedMessage) -> list[dict[str, Any]]:
    """A reply's blocks as the browser parts the page folds into its message."""
    parts: list[dict[str, Any]] = []
    for index, block in enumerate(message.blocks):
        lane = f"{message.source_id}:{index}"
        kind = block["type"]
        if kind in ("text", "thinking"):
            prefix = "text" if kind == "text" else "reasoning"
            parts += [
                {"type": f"{prefix}-start", "id": lane},
                {"type": f"{prefix}-delta", "id": lane, "delta": str(block.get(kind) or "")},
                {"type": f"{prefix}-end", "id": lane},
            ]
        elif kind == "tool_use":
            parts.append({
                "type": "tool-input-available",
                "toolCallId": str(block.get("id") or ""),
                "toolName": str(block.get("name") or ""),
                "input": deepcopy(block.get("input")),
                "dynamic": True,
            })
        elif kind == "tool_result" and block.get("is_error"):
            parts.append({
                "type": "tool-output-error",
                "toolCallId": str(block.get("tool_use_id") or ""),
                "errorText": str(block.get("content") or ""),
            })
        elif kind == "tool_result":
            parts.append({
                "type": "tool-output-available",
                "toolCallId": str(block.get("tool_use_id") or ""),
                "output": deepcopy(block.get("content")),
            })
        else:
            source = block["source"]
            parts.append({
                "type": "file",
                "mediaType": source.get("media_type"),
                "url": f"data:{source.get('media_type')};base64,{source.get('data')}",
            })
    return parts


def _entry(message: ImportedMessage, *, position: int) -> HistoryEntry:
    payload = message.payload()
    record: dict[str, Any] = {"source_id": message.source_id, "digest": payload["digest"], "position": position}
    if message.parent_source_id is not None:
        record["parent_source_id"] = message.parent_source_id
    if message.role == "user":
        return HistoryEntry(
            source_id=message.source_id,
            role="user",
            created_at=message.created_at,
            record=record,
            content="\n".join(str(block.get("text") or "") for block in message.blocks if block["type"] == "text"),
            content_blocks=[dict(block) for block in message.blocks],
            parent_source_id=message.parent_source_id,
        )
    record["completion"] = message.completion
    record["parts"] = reply_parts(message)
    return HistoryEntry(
        source_id=message.source_id,
        role="assistant",
        created_at=message.created_at,
        record=record,
        outcome="completed" if message.completion == COMPLETION_COMPLETE else None,
        parent_source_id=message.parent_source_id,
    )


class SessionHistoryImporter:
    """Store validated history in one session, idempotently."""

    def __init__(self, facts: SessionRuntimeRepository) -> None:
        self._facts = facts

    async def import_messages(
        self,
        session_id: str,
        messages: list[ImportedMessage],
    ) -> dict[str, int]:
        stored = await self._facts.imported_history(session_id)
        imported = {str(row["record"]["source_id"]): row["record"] for row in stored}
        stored_order = [str(row["record"]["source_id"]) for row in stored]
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
        if not new:
            return {"imported": 0, "unchanged": len(matched)}
        entries = [_entry(message, position=len(stored) + index) for index, message in enumerate(new)]
        try:
            stored_new = await self._facts.import_history(session_id, entries)
        except UniqueViolation as exc:
            # Another import stored one of these source ids first.
            raise self._conflict("another import of this history is in progress") from exc
        if not stored_new:
            raise APIError(
                code="HISTORY_IMPORT_AFTER_CONVERSATION",
                message=(
                    "this session already has messages of its own; earlier history "
                    "can only be imported before the conversation starts"
                ),
                status_code=409,
            )
        return {"imported": len(new), "unchanged": len(matched)}

    @staticmethod
    def _conflict(message: str) -> APIError:
        return APIError(code="HISTORY_IMPORT_CONFLICT", message=message, status_code=409)
