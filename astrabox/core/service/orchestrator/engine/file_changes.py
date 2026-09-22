"""File-change presentation seam, authored from completed supplier results.

Adapters own operation and completion semantics. Orchestration persists this
ordinary public data part; the console understands only these presentation
formats, never a vendor's tool arguments. A missing diff is explicit, not an
empty file manufactured as the before image.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict


class PatchHunk(TypedDict):
    oldStart: int
    oldLines: int
    newStart: int
    newLines: int
    lines: list[str]


@dataclass(frozen=True)
class ContentDiff:
    before: str
    after: str
    format: Literal["contents"] = "contents"


@dataclass(frozen=True)
class UnifiedDiff:
    patch: str
    format: Literal["unified"] = "unified"


@dataclass(frozen=True)
class HunkDiff:
    hunks: list[PatchHunk]
    format: Literal["hunks"] = "hunks"


@dataclass(frozen=True)
class ExcerptDiff:
    excerpts: list[ContentDiff]
    format: Literal["excerpts"] = "excerpts"


@dataclass(frozen=True)
class FileChange:
    path: str
    diff: ContentDiff | UnifiedDiff | HunkDiff | ExcerptDiff | None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("completed file change requires a path")


def file_changes_frame(
    tool_call_id: str, tool_name: str, changes: list[FileChange],
) -> dict[str, Any]:
    """Publish a stable result identity for live delivery and cold replay."""
    if not tool_call_id or not tool_name or not changes:
        raise ValueError("file changes require tool identity and changed files")
    return {
        "type": "data-file-changes",
        "id": f"file-changes:{tool_call_id}",
        "data": {
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "files": [asdict(change) for change in changes],
        },
    }
