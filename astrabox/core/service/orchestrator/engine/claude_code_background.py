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


def _is_subagent_context(raw: dict[str, Any]) -> bool:
    """A message that belongs to a subagent's own conversation, either spelling.

    The runner serializes the SDK's ``parent_tool_use_id`` field; a CLI JSONL
    line marks the same fact as ``isSidechain``. Everything in this module must
    treat such messages as invisible: a subagent that backgrounds its own Bash
    emits the same ``async_launched`` receipt and the same task lifecycle as a
    parent-lane Agent launch, and it lands in whatever turn's frame window is
    open at that moment. Including it would let a foreground manifest claim a
    sidechain task whose terminal is delivered only to that sidechain.
    """
    if str(raw.get("parent_tool_use_id") or "").strip():
        return True
    return raw.get("isSidechain") is True


def _extract_task_lifecycle(raw: dict[str, Any]) -> dict[str, str] | None:
    if _is_subagent_context(raw):
        return None
    raw_type = str(raw.get("type") or "").strip().lower()
    sdk_type = str(raw.get("__sdk_type") or "").strip()
    if raw_type != "system" and sdk_type not in _LIFECYCLE_MESSAGE_SDK_TYPES:
        return _extract_task_notification_record(raw)
    subtype = str(raw.get("subtype") or "").strip()
    if subtype not in _TASK_LIFECYCLE_SUBTYPES:
        return None
    status = str(raw.get("status") or "").strip().lower()
    if subtype == "task_updated":
        patch = raw.get("patch")
        if isinstance(patch, dict):
            status = str(patch.get("status") or status).strip().lower()
    return {
        "subtype": subtype,
        "task_id": str(raw.get("task_id") or "").strip(),
        "tool_use_id": str(raw.get("tool_use_id") or "").strip(),
        "status": status,
        "summary": str(raw.get("summary") or "").strip(),
        "result": "",
    }


def _task_notification_content(raw: dict[str, Any]) -> str:
    raw_type = str(raw.get("type") or "").strip().lower()
    if raw_type == "queue-operation":
        return str(raw.get("content") or "").strip()
    if raw_type == "user":
        origin = raw.get("origin")
        if not isinstance(origin, dict):
            return ""
        if str(origin.get("kind") or "").strip() != "task-notification":
            return ""
        message = raw.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content.strip() if isinstance(content, str) else ""
    return ""


def _extract_task_notification_record(raw: dict[str, Any]) -> dict[str, str] | None:
    content = _task_notification_content(raw)
    if not content.startswith("<task-notification"):
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    if root.tag != "task-notification":
        return None

    def _text(tag: str) -> str:
        value = root.findtext(tag)
        return str(value or "").strip()

    task_ids = [str(node.text or "").strip() for node in root.findall("task-id")]
    status = _text("status").lower()
    if (
        task_ids
        and all(task_ids)
        and len(set(task_ids)) == len(task_ids)
        and not root.findall("tool-use-id")
        and len(root.findall("status")) == 1
        and len(root.findall("summary")) <= 1
        and status == "stopped"
    ):
        # The SDK's aggregate stop is control metadata, not a per-child
        # terminal. Each child's native Store still owns its final state.
        return None
    if len(task_ids) > 1:
        raise ChildRunProjectionError(
            "Claude task-notification names "
            f"{len(task_ids)} tasks ({', '.join(task_ids)}); this projection "
            "settles one task per notification"
        )

    return {
        "subtype": "task_notification",
        "task_id": _text("task-id"),
        "tool_use_id": _text("tool-use-id"),
        "status": status,
        "summary": _text("summary"),
        "result": _text("result"),
    }


def _is_terminal_task_lifecycle(lifecycle: dict[str, str]) -> bool:
    return (
        lifecycle["subtype"] in _TERMINAL_TASK_LIFECYCLE_SUBTYPES
        and lifecycle["status"] in TERMINAL_TASK_STATUSES
    )


def build_background_task_manifest(raw_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Declare the detached Claude children this turn left open.

    Successful Agent and SendMessage receipts name the native local Agent;
    its task lifecycle and SessionStore use that identity across invocations.
    A child that also closes within this turn opens no continuation.
    """
    identities = ClaudeChildIdentities()
    pending_engine_refs: set[str] = set()
    activation_by_agent: dict[str, str] = {}
    observed_activations: dict[str, str] = {}

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        engine_ref = identities.observe(raw, include_messages=False)
        if engine_ref:
            pending_engine_refs.add(engine_ref)
            receipt = claude_tool_receipt(raw)
            if receipt is None:
                raise ChildRunProjectionError("Claude detached Agent has no activation receipt")
            activation_by_agent[engine_ref] = receipt[0]

        lifecycle = _extract_task_lifecycle(raw)
        if lifecycle is None:
            continue
        task_id = lifecycle["task_id"]
        activation = _lifecycle_activation(lifecycle, observed_activations)
        if lifecycle["subtype"] in {"task_started", "task_progress"}:
            # Mapping enrichment only — never an opening. A started/progress
            # record names a task; it does not say this turn launched it: the
            # same lifecycle arrives for a previous turn's task still running,
            # and for a task a subagent launched inside its own conversation —
            # and the SDK's TaskStartedMessage/TaskProgressMessage carry no
            # parent-context field at all, so the subagent filter above cannot
            # tell those apart. Opening from lifecycle would let a zero-tool
            # foreground turn claim a subagent's inner Bash task and project
            # BACKGROUND_RUNNING forever; the launch receipt is the only
            # statement that this turn owes a continuation.
            continue

        if (
            _is_terminal_task_lifecycle(lifecycle)
            and activation
            and activation == activation_by_agent.get(task_id)
        ):
            pending_engine_refs.discard(task_id)

    if not pending_engine_refs:
        return None
    pending_mapping = {agent_id: agent_id for agent_id in sorted(pending_engine_refs)}
    return {
        "transcript_refs": sorted(pending_engine_refs),
        "engine_refs": sorted(pending_engine_refs),
        "transcript_to_engine_ref": pending_mapping,
        "control_to_engine_ref": dict(pending_mapping),
        "activation_to_engine_ref": {
            activation_by_agent[agent_id]: agent_id for agent_id in sorted(pending_engine_refs)
        },
    }


def _lifecycle_activation(
    lifecycle: dict[str, str], observed_activations: dict[str, str],
) -> str:
    """Associate an id-less terminal with this source's observed activation."""
    task_id = lifecycle["task_id"]
    tool_use_id = lifecycle["tool_use_id"]
    if task_id and tool_use_id and lifecycle["subtype"] in {"task_started", "task_progress"}:
        observed_activations[task_id] = tool_use_id
    return tool_use_id or observed_activations.get(task_id, "")


def background_terminal_fact_for_manifest(
    raw: dict[str, Any],
    *,
    transcript_refs: set[str],
    engine_refs: set[str],
    transcript_to_engine_ref: dict[str, str],
    activation_to_engine_ref: dict[str, str],
    observed_activations: dict[str, str],
    control_to_engine_ref: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Match this activation's terminal without replacing the native Agent id."""
    lifecycle = _extract_task_lifecycle(raw)
    if lifecycle is None:
        return None

    task_id = lifecycle["task_id"]
    activation = _lifecycle_activation(lifecycle, observed_activations)
    if not _is_terminal_task_lifecycle(lifecycle):
        return None
    if not task_id or task_id not in engine_refs:
        return None
    if not activation or activation_to_engine_ref.get(activation) != task_id:
        return None

    transcript_ref = next(
        (
            candidate
            for candidate, mapped_engine_ref in transcript_to_engine_ref.items()
            if mapped_engine_ref == task_id and candidate in transcript_refs
        ),
        "",
    )
    if not transcript_ref:
        raise ChildRunProjectionError(
            "Claude terminal matched an engine reference without a transcript reference"
        )

    summary = str(raw.get("summary") or "").strip()
    if not summary:
        patch = raw.get("patch")
        if isinstance(patch, dict):
            summary = str(patch.get("summary") or "").strip()
    if not summary:
        summary = str(lifecycle.get("summary") or "").strip()
    result = str(lifecycle.get("result") or "").strip()
    if sdk_type := str(raw.get("__sdk_type") or "").strip():
        if sdk_type == "TaskNotificationMessage":
            # The typed SDK terminal carries its durable human-readable output
            # as ``summary``; ``result`` exists only in the queued XML spelling.
            result = summary
        elif sdk_type == "TaskUpdatedMessage":
            patch = raw.get("patch")
            if isinstance(patch, dict):
                result = str(patch.get("result") or "").strip()

    return {
        "transcript_ref": transcript_ref,
        "engine_ref": task_id,
        "control_ref": task_id,
        "event": "closed",
        "engine_event": lifecycle["subtype"],
        "engine_status": lifecycle["status"],
        "summary": summary,
        "result": result,
    }


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

    visit(normalized_root_id, "", 1)

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
