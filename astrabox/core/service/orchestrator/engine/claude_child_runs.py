"""Claude Code vocabulary at the engine-neutral child-run seam."""

from __future__ import annotations

import json
from typing import Any

from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
)

CLAUDE_CODE_ENGINE_KIND = "claude_code"

_LIFECYCLE_SUBTYPES = frozenset(
    {"task_started", "task_progress", "task_notification", "task_updated"}
)
_LIFECYCLE_SDK_TYPES = frozenset(
    {
        "TaskStartedMessage",
        "TaskProgressMessage",
        "TaskNotificationMessage",
        "TaskUpdatedMessage",
    }
)
_MESSAGE_ROLES = {
    "AssistantMessage": "assistant",
    "UserMessage": "user",
}
# Claude Code 2.1.220 calls the foreground child tool Agent.
_CHILD_LAUNCH_TOOL_NAMES = frozenset({"Agent"})
_SDK_BLOCK_TYPES = {
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
}


def claude_envelope_value(raw: dict[str, Any], key: str) -> Any:
    """Read one SDK field without discarding the dataclass's raw payload.

    The runner serializes a typed task dataclass. Its named fields live at the
    top level while the CLI's complete original event remains under ``data``.
    New vendor fields therefore appear under ``data`` before the SDK exposes a
    dataclass attribute for them.
    """

    value = raw.get(key)
    if value is not None:
        return value
    native = raw.get("data")
    if isinstance(native, dict):
        return native.get(key)
    return None


def claude_lifecycle_subtype(raw: dict[str, Any]) -> str:
    subtype = str(claude_envelope_value(raw, "subtype") or "").strip()
    sdk_type = str(raw.get("__sdk_type") or "").strip()
    raw_type = str(claude_envelope_value(raw, "type") or "").strip().lower()
    if subtype not in _LIFECYCLE_SUBTYPES:
        return ""
    if raw_type == "system" or sdk_type in _LIFECYCLE_SDK_TYPES:
        return subtype
    return ""


def claude_message_role(raw: dict[str, Any]) -> str:
    raw_type = str(raw.get("type") or "").strip().lower()
    if raw_type in {"assistant", "user"}:
        return raw_type
    return _MESSAGE_ROLES.get(str(raw.get("__sdk_type") or "").strip(), "")


def claude_child_context_id(raw: dict[str, Any]) -> str:
    return str(claude_envelope_value(raw, "parent_tool_use_id") or "").strip()


def claude_lifecycle_child_run_id(raw: dict[str, Any]) -> str:
    return str(claude_envelope_value(raw, "task_id") or "").strip()


def claude_tool_receipt(raw: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Read the native result and its invocation from either SDK serialization."""
    if claude_message_role(raw) != "user":
        return None
    results = [block for block in claude_content_blocks(raw) if block.get("type") == "tool_result"]
    result = raw.get("tool_use_result", raw.get("toolUseResult"))
    if not isinstance(result, dict):
        if len(results) != 1 or results[0].get("is_error") is True:
            return None
        content = results[0].get("content")
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
            content = content[0].get("text")
        if not isinstance(content, str):
            return None
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(result, dict) or not result.get("resumedAgentId"):
            return None
    if (
        result.get("status") == "async_launched"
        and not result.get("agentId")
        and not result.get("taskId")
        and not (claude_child_context_id(raw) or raw.get("isSidechain") is True)
    ):
        raise ChildRunProjectionError("Claude async_launched result lacks agentId")
    is_agent_result = result.get("status") in {"async_launched", "completed"} and result.get(
        "agentId"
    )
    is_resumed_result = result.get("success") is True and result.get("resumedAgentId")
    if not (is_agent_result or is_resumed_result):
        return None
    if len(results) != 1 or not str(results[0].get("tool_use_id") or "").strip():
        raise ChildRunProjectionError("Claude Agent receipt requires exactly one tool_result id")
    if results[0].get("is_error") is True:
        return None
    return str(results[0]["tool_use_id"]).strip(), result


class ClaudeChildIdentities:
    """Native task identities and invocation aliases at the child-run seam.

    A lifecycle task reference is not a local SessionStore reference. Only an
    Agent receipt supplies the latter; remote tasks and workflows keep their
    own native task IDs without entering local Agent materialization.
    """

    def __init__(self) -> None:
        self.agent_by_tool: dict[str, str] = {}
        self.task_types: dict[str, str] = {}
        self.parent_by_tool: dict[str, str] = {}
        self.parent_by_agent: dict[str, str] = {}
        self.send_message_targets: dict[str, str] = {}

    def bind(self, tool_id: str, agent_id: str) -> None:
        if not tool_id or not agent_id:
            raise ChildRunProjectionError("Claude child association requires tool and Agent ids")
        existing = self.agent_by_tool.get(tool_id)
        if existing and existing != agent_id:
            raise ChildRunProjectionError(
                f"Claude tool names conflicting Agents tool_id={tool_id!r} agents={existing!r},{agent_id!r}"
            )
        self.agent_by_tool[tool_id] = agent_id
        parent = self.parent_by_tool.get(tool_id)
        if parent:
            self.bind_parent(agent_id, parent)

    def bind_parent(self, agent_id: str, parent_id: str) -> None:
        existing = self.parent_by_agent.get(agent_id)
        if existing and existing != parent_id:
            raise ChildRunProjectionError(
                f"Claude child has conflicting parents agent_id={agent_id!r} parents={existing!r},{parent_id!r}"
            )
        self.parent_by_agent[agent_id] = parent_id

    def message_ref(self, raw: dict[str, Any]) -> str:
        tool_id = claude_child_context_id(raw)
        agent_id = str(raw.get("agentId") or "").strip()
        if agent_id and tool_id:
            self.bind(tool_id, agent_id)
        if not tool_id and not (raw.get("isSidechain") is True and agent_id):
            return ""
        if agent_id:
            return agent_id
        if tool_id not in self.agent_by_tool:
            raise ChildRunProjectionError(
                f"Claude child message has no native Agent association tool_id={tool_id!r}"
            )
        return self.agent_by_tool[tool_id]

    def lifecycle_ref(self, raw: dict[str, Any]) -> str:
        task_id = claude_lifecycle_child_run_id(raw)
        # The SDK's task_type is optional and its catalog is not limited to
        # local Agents. Bash/Monitor work is the measured non-Agent case;
        # other task lifecycles retain their native identity and vocabulary.
        if self.task_types.get(task_id) == "local_bash":
            return ""
        return task_id

    def observe(self, raw: dict[str, Any], *, include_messages: bool = True) -> str | None:
        """Index native facts; return an Agent receipt that starts detached work."""
        if claude_lifecycle_subtype(raw):
            task_id = claude_lifecycle_child_run_id(raw)
            task_type = str(claude_envelope_value(raw, "task_type") or "").strip()
            if task_id and task_type:
                existing_type = self.task_types.get(task_id)
                if existing_type and existing_type != task_type:
                    raise ChildRunProjectionError(f"Claude task changed type task_id={task_id!r}")
                self.task_types[task_id] = task_type
            tool_id = str(claude_envelope_value(raw, "tool_use_id") or "").strip()
            if tool_id and self.lifecycle_ref(raw):
                self.bind(tool_id, task_id)
            return None

        for block in claude_content_blocks(raw):
            if block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                tool_input = block.get("input")
                target = (
                    str(tool_input.get("to") or "").strip() if isinstance(tool_input, dict) else ""
                )
                tool_id = str(block.get("id") or "").strip()
                if tool_id and target:
                    self.send_message_targets[tool_id] = target

        receipt = claude_tool_receipt(raw)
        detached_id: str | None = None
        if receipt is not None:
            tool_id, result = receipt
            agent_id = str(result.get("agentId") or "").strip()
            resumed_id = str(result.get("resumedAgentId") or "").strip()
            if resumed_id and result.get("success") is True:
                target = self.send_message_targets.get(tool_id)
                if target is None:
                    raise ChildRunProjectionError(
                        "Claude resumed Agent receipt has no SendMessage call"
                    )
                # `to` accepts a native Agent name as well as its id. The
                # successful receipt is authoritative when a name was used.
                if target in self.agent_by_tool.values() and target != resumed_id:
                    raise ChildRunProjectionError("Claude SendMessage resumed a different Agent")
                agent_id = resumed_id
                detached_id = agent_id
            elif agent_id and result.get("status") == "async_launched":
                detached_id = agent_id
            if agent_id:
                self.bind(tool_id, agent_id)
                existing_type = self.task_types.get(agent_id)
                if existing_type and existing_type != "local_agent":
                    raise ChildRunProjectionError("Claude Agent receipt names a non-Agent task")
                self.task_types[agent_id] = "local_agent"

        spawned_tools = claude_spawned_child_run_ids(claude_content_blocks(raw))
        if (
            include_messages
            and spawned_tools
            and (claude_child_context_id(raw) or raw.get("isSidechain") is True)
        ):
            parent_id = self.message_ref(raw)
            for tool_id in spawned_tools:
                existing = self.parent_by_tool.get(tool_id)
                if existing and existing != parent_id:
                    raise ChildRunProjectionError("Claude Agent invocation has conflicting parents")
                self.parent_by_tool[tool_id] = parent_id
                if child_id := self.agent_by_tool.get(tool_id):
                    self.bind_parent(child_id, parent_id)
        if claude_child_context_id(raw) or raw.get("isSidechain") is True:
            return None
        return detached_id

    @classmethod
    def from_messages(cls, raw_items: list[dict[str, Any]]) -> ClaudeChildIdentities:
        identities = cls()
        for raw in raw_items:
            identities.observe(raw, include_messages=False)
        for raw in raw_items:
            identities.observe(raw)
        return identities


def claude_lifecycle_parent_child_run_id(raw: dict[str, Any]) -> str:
    """Return explicit lineage when the CLI includes it.

    The pinned SDK's typed lifecycle classes currently do not expose this
    field. ``claude_spawned_child_run_ids`` supplies the measured path for
    nested foreground Agents; this reader preserves a future/native explicit
    field without teaching the platform its name.
    """

    return str(claude_envelope_value(raw, "parent_tool_use_id") or "").strip()


def claude_content_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    content = raw.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        blocks = [dict(block) for block in content if isinstance(block, dict)]
        return [_canonical_content_block(block) for block in blocks]
    message = raw.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return [{"type": "text", "text": message["content"]}]
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        blocks = [dict(block) for block in message["content"] if isinstance(block, dict)]
        return [_canonical_content_block(block) for block in blocks]
    return []


def _canonical_content_block(block: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(block)
    if not str(canonical.get("type") or "").strip():
        block_type = _SDK_BLOCK_TYPES.get(str(canonical.get("__sdk_type") or "").strip(), "")
        if block_type:
            canonical["type"] = block_type
    canonical.pop("__sdk_type", None)
    if canonical.get("type") == "tool_result":
        # SDK 0.2.152 parses these optional fields with dict.get(); its
        # dataclass serializer emits None where the native Store omits them.
        # Do not erase explicit booleans or nulls inside the tool's payload.
        for field in ("content", "is_error"):
            if canonical.get(field) is None:
                canonical.pop(field, None)
    return canonical


def claude_spawned_child_run_ids(
    content: list[dict[str, Any]],
) -> list[str]:
    """Child ids launched by one Claude child-context assistant message."""

    child_ids: list[str] = []
    for block in content:
        block_type = str(block.get("type") or "").strip()
        if not block_type:
            block_type = _SDK_BLOCK_TYPES.get(str(block.get("__sdk_type") or "").strip(), "")
        if block_type != "tool_use":
            continue
        if str(block.get("name") or "").strip() not in _CHILD_LAUNCH_TOOL_NAMES:
            continue
        child_run_id = str(block.get("id") or "").strip()
        if child_run_id and child_run_id not in child_ids:
            child_ids.append(child_run_id)
    return child_ids


def infer_claude_child_run_parents(
    blocks: list[dict[str, Any]],
    *,
    identities: ClaudeChildIdentities,
) -> list[dict[str, Any]]:
    """Stamp nested lineage from Claude child messages, independent of order."""

    parent_by_child: dict[str, str] = {}
    for block in blocks:
        if str(block.get("type") or "").strip() != "subagent":
            continue
        data = block.get("data")
        if not isinstance(data, dict) or str(data.get("kind") or "") != "message":
            continue
        parent_engine_ref = str(data.get("engineRef") or "").strip()
        content = data.get("content")
        if not parent_engine_ref or not isinstance(content, list):
            continue
        for tool_id in claude_spawned_child_run_ids(
            [dict(item) for item in content if isinstance(item, dict)]
        ):
            engine_ref = identities.agent_by_tool.get(tool_id)
            if not engine_ref:
                # An Agent request can fail without creating a native child.
                continue
            existing = parent_by_child.get(engine_ref)
            if existing and existing != parent_engine_ref:
                raise ChildRunProjectionError(
                    "Claude child run has conflicting launch parents "
                    f"engine_ref={engine_ref!r} "
                    f"parents={existing!r},{parent_engine_ref!r}"
                )
            parent_by_child[engine_ref] = parent_engine_ref

    linked: list[dict[str, Any]] = []
    for raw_block in blocks:
        block = dict(raw_block)
        data_raw = block.get("data")
        if str(block.get("type") or "").strip() == "subagent" and isinstance(data_raw, dict):
            data = dict(data_raw)
            engine_ref = str(data.get("engineRef") or "").strip()
            inferred_parent = parent_by_child.get(engine_ref, "")
            explicit_parent = str(data.get("parentEngineRef") or "").strip()
            if explicit_parent and inferred_parent and explicit_parent != inferred_parent:
                raise ChildRunProjectionError(
                    "Claude child run explicit and inferred parents disagree "
                    f"engine_ref={engine_ref!r} "
                    f"parents={explicit_parent!r},{inferred_parent!r}"
                )
            if inferred_parent:
                data["parentEngineRef"] = inferred_parent
            block["data"] = data
        linked.append(block)
    return linked
