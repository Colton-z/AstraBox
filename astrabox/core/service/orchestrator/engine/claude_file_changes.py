"""Claude SDK structured tool results projected onto the file-change seam."""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.engine.file_changes import (
    ContentDiff, FileChange, HunkDiff, file_changes_frame,
)


def claude_file_changes(
    tool_call_id: str, result: dict[str, Any],
) -> dict[str, Any] | None:
    # These are the SDK's FileWriteOutput and FileEditOutput discriminants,
    # not an inference from the tool's requested arguments.
    is_write = result.get("type") in {"create", "update"}
    is_edit = "oldString" in result and "newString" in result
    if not is_write and not is_edit:
        return None
    path = result["filePath"]
    hunks = result.get("structuredPatch")
    diff: ContentDiff | HunkDiff | None = None
    if hunks:
        diff = HunkDiff(hunks=hunks)
    elif is_write and result["type"] == "create":
        diff = ContentDiff(before="", after=result["content"])
    elif is_write and isinstance(result.get("originalFile"), str):
        diff = ContentDiff(before=result["originalFile"], after=result["content"])
    return file_changes_frame(tool_call_id, "Write" if is_write else "Edit", [
        FileChange(path=path, diff=diff),
    ])
