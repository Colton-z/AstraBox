"""Fold stored browser parts into AI SDK UI messages for the page's history.

The page renders a live response by feeding the stream's chunks to the AI
SDK, which folds them into one ``UIMessage``. History is the same chunks read
back from the output log, so the server folds them the same way
(``processUIMessageStream`` in the AI SDK): a page that loads history and a
page that watched the response live show the same parts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from astrabox.core.service.orchestrator.engine.interaction_contract import public_interaction_view


def fold_message(message_id: str, chunks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One assistant ``UIMessage`` from a response's chunks, in order."""
    parts: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    texts: dict[str, dict[str, Any]] = {}
    reasonings: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    data_parts: dict[tuple[str, str], dict[str, Any]] = {}
    part: Any

    def tool(chunk: Mapping[str, Any]) -> dict[str, Any]:
        call_id = str(chunk.get("toolCallId") or "")
        part = tools.get(call_id)
        if part is None:
            name = str(chunk.get("toolName") or "")
            part = {
                "type": "dynamic-tool" if chunk.get("dynamic") else f"tool-{name}",
                "toolCallId": call_id,
                "state": "input-streaming",
                "input": None,
            }
            if chunk.get("dynamic"):
                part["toolName"] = name
            tools[call_id] = part
            parts.append(part)
        return part

    for chunk in chunks:
        kind = str(chunk.get("type") or "")
        if kind in ("start", "finish", "message-metadata"):
            if isinstance(chunk.get("messageMetadata"), dict):
                metadata.update(deepcopy(chunk["messageMetadata"]))
        elif kind == "start-step":
            parts.append({"type": "step-start"})
        elif kind == "finish-step":
            texts.clear()
            reasonings.clear()
        elif kind in ("text-start", "reasoning-start"):
            part = {"type": "text" if kind == "text-start" else "reasoning", "text": "", "state": "streaming"}
            if isinstance(chunk.get("providerMetadata"), dict):
                part["providerMetadata"] = deepcopy(chunk["providerMetadata"])
            (texts if kind == "text-start" else reasonings)[str(chunk.get("id") or "")] = part
            parts.append(part)
        elif kind in ("text-delta", "reasoning-delta"):
            lane = texts if kind == "text-delta" else reasonings
            part = lane.get(str(chunk.get("id") or ""))
            if part is not None:
                part["text"] += str(chunk.get("delta") or "")
        elif kind in ("text-end", "reasoning-end"):
            lane = texts if kind == "text-end" else reasonings
            part = lane.get(str(chunk.get("id") or ""))
            if part is not None:
                part["state"] = "done"
        elif kind == "tool-input-start":
            tool(chunk)
        elif kind == "tool-input-available":
            part = tool(chunk)
            part.update(state="input-available", input=deepcopy(chunk.get("input")))
        elif kind == "tool-input-error":
            part = tool(chunk)
            part.update(
                state="output-error", input=deepcopy(chunk.get("input")), errorText=str(chunk.get("errorText") or "")
            )
        elif kind == "tool-approval-request":
            part = tools.get(str(chunk.get("toolCallId") or ""))
            if part is not None:
                part.update(state="approval-requested", approval={"id": chunk.get("approvalId")})
        elif kind == "tool-approval-response":
            for part in tools.values():
                approval = part.get("approval")
                if isinstance(approval, dict) and approval.get("id") == chunk.get("approvalId"):
                    approval["approved"] = chunk.get("approved")
                    if chunk.get("reason"):
                        approval["reason"] = chunk.get("reason")
                    part["state"] = "approval-responded"
        elif kind == "tool-output-available":
            part = tool(chunk)
            part.update(state="output-available", output=deepcopy(chunk.get("output")))
        elif kind == "tool-output-error":
            part = tool(chunk)
            part.update(state="output-error", errorText=str(chunk.get("errorText") or ""))
        elif kind == "tool-output-denied":
            tool(chunk)["state"] = "output-denied"
        elif kind in ("source-url", "source-document", "file"):
            parts.append({key: deepcopy(value) for key, value in chunk.items()})
        elif kind.startswith("data-"):
            if chunk.get("transient") is True:
                continue
            part = {"type": kind, "data": deepcopy(chunk.get("data"))}
            part_id = chunk.get("id")
            if part_id is not None:
                part["id"] = part_id
                existing = data_parts.get((kind, str(part_id)))
                if existing is not None:
                    existing["data"] = part["data"]
                    continue
                data_parts[(kind, str(part_id))] = part
            parts.append(part)
    open_parts = {
        id(part): part_id
        for lane in (texts, reasonings)
        for part_id, part in lane.items()
        if part.get("state") == "streaming"
    }
    if open_parts:
        metadata["continuation_parts"] = [
            {"index": index, "id": open_parts[id(part)]} for index, part in enumerate(parts) if id(part) in open_parts
        ]
    for part in parts:
        if part.get("state") == "streaming":
            part["state"] = "done"
    return {"id": message_id, "role": "assistant", "parts": parts, "metadata": metadata}


def input_message(row: Mapping[str, Any]) -> dict[str, Any]:
    """The person's message for one accepted input."""
    parts: list[dict[str, Any]] = []
    blocks = row.get("content_blocks")
    if isinstance(blocks, list) and blocks:
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append({"type": "text", "text": str(block.get("text") or "")})
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append(_image_file_part(block))
    else:
        parts.append({"type": "text", "text": str(row.get("content") or "")})
    metadata = {
        "input_id": row.get("input_id"),
        "created_at": row.get("created_at"),
        "state": row.get("state"),
        "author_user_id": row.get("author_user_id"),
        "source": row.get("source"),
    }
    if row.get("failure"):
        metadata["failure"] = deepcopy(row["failure"])
    if row.get("import_record"):
        metadata["history_import"] = _provenance(row["import_record"])
    return {"id": str(row.get("client_message_id") or row.get("input_id")), "role": "user", "parts": parts, "metadata": metadata}


def _image_file_part(block: Mapping[str, Any]) -> dict[str, Any]:
    """An accepted image block as the AI SDK's file part, its bytes in a data URL.

    The input stores the block it was sent with (``engine/input_content.py``
    accepts only base64 sources), so the page shows the image it sent.
    """
    source = block.get("source") if isinstance(block.get("source"), Mapping) else {}
    media_type = str(source.get("media_type") or "")
    return {"type": "file", "mediaType": media_type, "url": f"data:{media_type};base64,{source.get('data') or ''}"}


def _provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    """Where an imported message came from: its source id, parent and completion."""
    return {
        key: record[key]
        for key in ("source_id", "completion", "parent_source_id")
        if record.get(key) is not None
    }


def history_messages(
    page: Mapping[str, Any],
    outputs: Iterable[Mapping[str, Any]],
    interactions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The page's messages for one history page, in conversation order.

    Interaction settlement comes from the durable rows, independently of
    whether the tool returned output before its response ended.
    """
    settlements = {str(row["interaction_id"]): row for row in interactions}
    decisions = {
        key: row["answer"] for key, row in settlements.items()
        if row.get("answer") is not None and row["status"] in {"open", "answered"}
    }
    by_response: dict[str, list[dict[str, Any]]] = {}
    for row in outputs:
        response_id = row.get("response_id")
        if response_id:
            by_response.setdefault(str(response_id), []).append(row["part"])
    inputs = {int(row["seq"]): row for row in page["inputs"]}
    responses = {int(row["seq"]): row for row in page["responses"]}
    messages: list[dict[str, Any]] = []
    for entry in page["entries"]:
        if entry["kind"] == "input":
            messages.append(input_message(inputs[entry["seq"]]))
            continue
        response = responses[entry["seq"]]
        record = response.get("import_record")
        # An imported reply's parts were never output; they live on its row.
        chunks = record["parts"] if record else by_response.get(str(response["response_id"]), ())
        message = fold_message(str(response["response_id"]), chunks)
        _apply_approval_decisions(message, decisions)
        for part in message["parts"]:
            if part["type"] != "data-interaction":
                continue
            data = part["data"]
            interaction_id = str(data["interaction_id"])
            row = settlements.get(interaction_id)
            if row is None:
                raise RuntimeError(f"History interaction has no durable record: {interaction_id}")
            data["status"] = row["status"]
            if row["status"] == "answered" and row.get("answer") is not None:
                data["answer"] = deepcopy(row["answer"])
        if record:
            message["metadata"]["history_import"] = _provenance(record)
        message["metadata"].update(
            {
                "response_id": response["response_id"],
                "origin": response["origin"],
                "status": response["status"],
                "outcome": response.get("outcome"),
                "native_reason": response.get("native_reason"),
                "started_at": response.get("started_at"),
                "ended_at": response.get("ended_at"),
            }
        )
        if response.get("error"):
            message["metadata"]["error"] = deepcopy(response["error"])
        messages.append(message)
    return messages


def _apply_approval_decisions(message: dict[str, Any], decisions: Mapping[str, Mapping[str, Any]]) -> None:
    """Put each person's tool-approval decision on the tool call it was about.

    The decision is recorded on the interaction, not in the output, so the
    page's own record of it does not survive a reload; this restores the
    approval exactly as the page records it. A call still waiting for the
    runtime reads as responded, and one that has run since keeps its state.
    """
    for part in message["parts"]:
        approval = part.get("approval")
        if not isinstance(approval, dict):
            continue
        answer = decisions.get(str(approval.get("id") or ""))
        if answer is None:
            continue
        approved = answer.get("decision") == "approve"
        approval["approved"] = approved
        comment = str(answer.get("comment") or "").strip()
        if not approved and comment:
            approval["reason"] = comment
        if part.get("state") == "approval-requested":
            part["state"] = "approval-responded"


#: A sub-agent's native handles, which only the platform uses to address it.
_ENGINE_CHILD_REFS = frozenset({"engineRef", "parentEngineRef", "controlRef"})


def public_part(part: Mapping[str, Any], *, shared: bool) -> dict[str, Any] | None:
    """One UI message part as a page receives it; ``None`` drops it.

    An interaction part carries the contract's public half only, and a share
    link's copy leaves out the tool input. Keys an adapter marks private
    (``__``-prefixed) never leave the server, and neither do the engine's own
    handles on a sub-agent: a child run is addressed by its public id through
    the child-runs API.
    """
    public = {key: value for key, value in part.items() if not str(key).startswith("__")}
    if public.get("type") == "data-subagent" and isinstance(public.get("data"), Mapping):
        public["data"] = {
            key: value for key, value in public["data"].items() if key not in _ENGINE_CHILD_REFS
        }
    if public.get("type") == "data-interaction":
        data = public_interaction_view(public.get("data"), include_tool_input=not shared)
        if data is None:
            return None
        public["data"] = data
    return public
