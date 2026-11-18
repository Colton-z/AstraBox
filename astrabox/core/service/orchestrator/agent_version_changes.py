"""What differs between two stored Agent definitions, field by field."""

from __future__ import annotations

from typing import Any


def definition_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Each value that differs between two definitions, as ``{path, before, after}``.

    Objects are compared key by key, so a change inside ``default_repo`` is
    reported at ``["default_repo", "url"]`` rather than as the whole object; an
    unset object compares as an empty one. Lists and scalars are compared whole.
    ``path`` is a list because the keys of ``mcp_servers`` and
    ``connector_servers`` are names an author chose and may contain dots.
    """
    changes: list[dict[str, Any]] = []
    _compare([], before, after, changes)
    return changes


def _compare(path: list[str], before: Any, after: Any, changes: list[dict[str, Any]]) -> None:
    if isinstance(before, dict) and after is None:
        after = {}
    elif isinstance(after, dict) and before is None:
        before = {}
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _compare([*path, str(key)], before.get(key), after.get(key), changes)
    elif before != after:
        changes.append({"path": path, "before": before, "after": after})
