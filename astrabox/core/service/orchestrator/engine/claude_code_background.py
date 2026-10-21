"""Claude Code's background-task vocabulary — read here, nowhere else.

Everything in this module is Claude Code's own: the ``async_launched`` tool
result the CLI answers a backgrounded ``Agent`` call with, the ``task_started``
/ ``task_progress`` / ``task_notification`` / ``task_updated`` system
subtypes, and the ``<task-notification>`` XML the CLI queues into the session.
None of it is a platform concept, and another engine has no reason to share a
single one of those names.

Across the engine seam, the platform asks which tasks opened, which records end
them, and how a settled task's opaque SessionStore scope projects into Agents
blocks. It gets back platform-shaped ids, terminal records, paths and blocks;
it never reads the vocabulary above itself. An engine with no background-task
concept answers the base adapter's empty values and the whole lane stays dark.

Every reader here accepts two spellings of the same event, because two kinds
of envelope arrive. A message the runner serialized itself carries
``__sdk_type`` (the agent SDK's classes are plain dataclasses with no type
field, so the runner stamps the class name at ``asdict`` time and field names
stay snake_case: ``tool_use_result``). A CLI JSONL line — what the transcript
mirror stores — keeps the CLI's own spelling: ``type: "user"``,
``toolUseResult``. Same data, two serializations, both the adapter's to know.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

# The vendor's own terminal set — imported, not re-declared, because the two
# lifecycle vocabularies disagree in a way that is easy to get wrong from
# memory: a ``task_notification`` reports a killed task as ``stopped``, while
# ``task_updated`` reports the raw ``killed`` (and the SDK warns the matching
# notification is sometimes suppressed, e.g. for TaskStop). A hand-written set
# that covers only one spelling leaves that task pending forever.
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES

from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
    canonicalize_child_run_blocks,
)
from astrabox.core.service.orchestrator.engine.claude_child_runs import (
    CLAUDE_CODE_ENGINE_KIND,
    ClaudeChildIdentities,
    claude_tool_receipt,
)
from astrabox.core.service.orchestrator.engine.claude_message_blocks import (
    parse_tool_event_blocks,
)

_TASK_LIFECYCLE_SUBTYPES = frozenset(
    {
        "task_started",
        "task_progress",
        "task_notification",
        "task_updated",
    }
)
_TERMINAL_TASK_LIFECYCLE_SUBTYPES = frozenset(
    {
        "task_notification",
        "task_updated",
    }
)
#: Runner-serialized lifecycle envelopes: the SDK parser returns these
#: SystemMessage subclasses for the subtypes above, and the runner stamps the
#: class name. The base name is included because the parser's contract is the
#: subtype, not the subclass.
_LIFECYCLE_MESSAGE_SDK_TYPES = frozenset(
    {
        "SystemMessage",
        "TaskStartedMessage",
        "TaskProgressMessage",
        "TaskNotificationMessage",
        "TaskUpdatedMessage",
    }
)
_AGENT_TRANSCRIPT_PREFIX = "subagents/agent-"


def _agent_scope(raw_scope: dict[str, Any]) -> dict[str, Any] | None:
    """Decode one Claude SessionStore Agent scope and its native metadata."""

    subpath = str(raw_scope.get("subpath") or "").strip()
    if not subpath.startswith(_AGENT_TRANSCRIPT_PREFIX):
        return None
    agent_id = subpath[len(_AGENT_TRANSCRIPT_PREFIX) :].strip()
    raw_entries = raw_scope.get("entries")
    if not agent_id or not isinstance(raw_entries, list):
        raise ChildRunProjectionError(
            f"Claude Agent transcript scope is malformed subpath={subpath!r}"
        )
    entries = [dict(entry) for entry in raw_entries if isinstance(entry, dict)]
    metadata = [entry for entry in entries if entry.get("type") == "agent_metadata"]
    if not metadata:
        raise ChildRunProjectionError(
            f"Claude Agent transcript lacks agent_metadata agent_id={agent_id!r}"
        )
    identity = metadata[-1]
    for previous in metadata[:-1]:
        if any(
            previous.get(key) != identity.get(key)
            for key in ("toolUseId", "parentAgentId", "spawnDepth")
        ):
            raise ChildRunProjectionError(
                f"Claude Agent metadata has conflicting identity or lineage agent_id={agent_id!r}"
            )
    tool_use_id = str(identity.get("toolUseId") or "").strip()
    if not tool_use_id:
        raise ChildRunProjectionError(
            f"Claude agent_metadata lacks toolUseId agent_id={agent_id!r}"
        )
    parent_agent_id = str(identity.get("parentAgentId") or "").strip()
    spawn_depth = identity.get("spawnDepth")
    if isinstance(spawn_depth, bool) or not isinstance(spawn_depth, int) or spawn_depth < 1:
        raise ChildRunProjectionError(
            "Claude agent_metadata has invalid spawnDepth "
            f"agent_id={agent_id!r} value={spawn_depth!r}"
        )
    return {
        "agent_id": agent_id,
        "tool_use_id": tool_use_id,
        "parent_agent_id": parent_agent_id,
        "spawn_depth": spawn_depth,
        "description": str(identity.get("description") or "").strip(),
        "agent_type": str(identity.get("agentType") or "").strip(),
        "entries": entries,
    }


def agent_stop_reason_is_active(stop_reason: str) -> bool:
    """A transcript with no settlement, or a tool-use continuation, has work."""
    return not stop_reason or stop_reason == "tool_use"


def stored_child_identities(
    main_entries: list[dict[str, Any]], raw_scopes: list[dict[str, Any]]
) -> ClaudeChildIdentities:
    """Restore native Agent aliases and lineage independently of journal retention."""
    identities = ClaudeChildIdentities.from_messages(main_entries)
    for raw_scope in raw_scopes:
        scope = _agent_scope(raw_scope)
        if scope is None:
            continue
        agent_id = str(scope["agent_id"])
        identities.bind(str(scope["tool_use_id"]), agent_id)
        parent_id = str(scope["parent_agent_id"])
        if parent_id:
            identities.bind_parent(agent_id, parent_id)
    return identities


def _agent_transcript_lifecycle(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """Return the structural edge and native stop reason in one Agent scope."""

    for raw in reversed(entries):
        if str(raw.get("type") or "").strip() != "assistant":
            continue
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        stop_reason = message.get("stop_reason")
        if stop_reason is None:
            continue
        normalized = str(stop_reason).strip()
        if not normalized:
            raise ChildRunProjectionError(
                "Claude child transcript carries an empty assistant stop_reason"
            )
        return (
            ("updated", normalized)
            if agent_stop_reason_is_active(normalized)
            else ("closed", normalized)
        )
    return "updated", ""


def _agent_message_blocks(
    scope: dict[str, Any],
    *,
    parent_engine_ref: str,
    identities: ClaudeChildIdentities,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw in scope["entries"]:
        for raw_block in parse_tool_event_blocks(
            {**raw, "parent_tool_use_id": scope["tool_use_id"]}, identities=identities
        ):
            block = dict(raw_block)
            data_raw = block.get("data")
            if (
                str(block.get("type") or "").strip() == "subagent"
                and isinstance(data_raw, dict)
                and str(data_raw.get("kind") or "").strip() == "message"
                and str(block.get("id") or "").strip()
            ):
                data = dict(data_raw)
                if parent_engine_ref:
                    data["parentEngineRef"] = parent_engine_ref
                block["data"] = data
                blocks.append(block)
    return blocks


def background_task_transcript_tree_blocks(
    raw_scopes: list[dict[str, Any]],
    *,
    root_transcript_ref: str,
    parent_engine_ref: str,
) -> list[dict[str, Any]]:
    """Project Claude's official Agent scopes into one durable child-run tree."""

    normalized_root_id = str(root_transcript_ref or "").strip()
    normalized_parent_id = str(parent_engine_ref or "").strip()
    if not normalized_root_id or not normalized_parent_id:
        return []

    scopes: dict[str, dict[str, Any]] = {}
    ordered_agent_ids: list[str] = []
    for raw_scope in raw_scopes:
        if not isinstance(raw_scope, dict):
            continue
        scope = _agent_scope(raw_scope)
        if scope is None:
            continue
        agent_id = str(scope["agent_id"])
        if agent_id in scopes:
            raise ChildRunProjectionError(
                f"Claude SessionStore contains duplicate Agent scope agent_id={agent_id!r}"
            )
        scopes[agent_id] = scope
        ordered_agent_ids.append(agent_id)

    root = scopes.get(normalized_root_id)
    if root is None:
        return []
    if root["agent_id"] != normalized_parent_id:
        raise ChildRunProjectionError(
            "Claude background Agent identity disagrees with its manifest "
            f"task_id={normalized_root_id!r} "
            f"manifest={normalized_parent_id!r} scope={root['agent_id']!r}"
        )

    identities = ClaudeChildIdentities()
    children: dict[str, list[str]] = {}
    for agent_id in ordered_agent_ids:
        identities.bind(str(scopes[agent_id]["tool_use_id"]), agent_id)
        parent_agent_id = str(scopes[agent_id]["parent_agent_id"])
        if parent_agent_id:
            identities.bind_parent(agent_id, parent_agent_id)
            children.setdefault(parent_agent_id, []).append(agent_id)

    ordered_tree: list[tuple[dict[str, Any], str, int]] = []
    visited: set[str] = set()

    def visit(agent_id: str, parent_run_id: str, depth: int) -> None:
        if agent_id in visited:
            raise ChildRunProjectionError(
                f"Claude Agent transcript parent cycle includes agent_id={agent_id!r}"
            )
        visited.add(agent_id)
        scope = scopes[agent_id]
        if scope["spawn_depth"] != depth:
            raise ChildRunProjectionError(
                "Claude Agent transcript depth disagrees with its parent graph "
                f"agent_id={agent_id!r} metadata={scope['spawn_depth']!r} graph={depth}"
            )
        ordered_tree.append((scope, parent_run_id, depth))
        child_parent_run_id = str(scope["agent_id"])
        for child_agent_id in children.get(agent_id, []):
            visit(child_agent_id, child_parent_run_id, depth + 1)

    visit(normalized_root_id, str(root["parent_agent_id"]), int(root["spawn_depth"]))

    blocks: list[dict[str, Any]] = []
    for scope, parent_run_id, depth in ordered_tree:
        engine_ref = str(scope["agent_id"])
        if depth > 1:
            event, engine_reason = _agent_transcript_lifecycle(scope["entries"])
            lifecycle_data: dict[str, Any] = {
                "kind": "lifecycle",
                "engineRef": engine_ref,
                "parentEngineRef": parent_run_id,
                "event": event,
                "engineEvent": "session_store.stop_reason",
                "operations": [],
            }
            if engine_reason:
                lifecycle_data["engineReason"] = engine_reason
            if scope["description"]:
                lifecycle_data["description"] = scope["description"]
            if scope["agent_type"]:
                lifecycle_data["taskType"] = scope["agent_type"]
            blocks.append(
                {
                    "type": "subagent",
                    "id": f"subagent:lifecycle:session-store:{scope['agent_id']}",
                    "data": canonical_child_run_data(
                        lifecycle_data,
                        engine_kind=CLAUDE_CODE_ENGINE_KIND,
                    ),
                }
            )
        blocks.extend(
            _agent_message_blocks(
                scope,
                parent_engine_ref=parent_run_id,
                identities=identities,
            )
        )
    return canonicalize_child_run_blocks(
        blocks,
        engine_kind=CLAUDE_CODE_ENGINE_KIND,
    )
