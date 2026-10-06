"""Deterministic prefix rules with per-viewer private subtrees."""

from __future__ import annotations

from typing import Any

from .limits import LEVELS
from .manifest import path_segments


def can_access(
    rules: list[dict[str, Any]],
    path: str,
    level: str,
    viewer_id: str | None,
    *,
    write: bool = False,
    guest: bool = False,
) -> bool:
    parts = path_segments(path)
    if level not in LEVELS or level == "none" or (write and guest):
        return False
    # Claude's data/users subtree is private even to an owner; only an explicit
    # rule at data/users opens it. Prefix matching also protects descendants.
    private_prefixes = [("data", "users")]
    private_prefixes.extend(
        tuple(path_segments(r["path"], rule=True)[:-1])
        for r in rules
        if r["path"].endswith("{self}")
    )
    for prefix in private_prefixes:
        if tuple(parts[: len(prefix)]) != prefix or len(parts) <= len(prefix):
            continue
        opened = any(r["path"] == "/".join(prefix) and {"read", "write"} <= set(r) for r in rules)
        if not opened and (guest or not viewer_id or parts[len(prefix)] != viewer_id):
            return False
    required = "interact" if write else "view"
    key = "write" if write else "read"
    for rule in sorted(rules, key=lambda r: len(path_segments(r["path"], rule=True))):
        rp = path_segments(rule["path"], rule=True)
        if rp and rp[-1] == "{self}":
            if guest or not viewer_id:
                continue
            rp = [*rp[:-1], viewer_id]
        if parts[: len(rp)] == rp:
            required = rule.get(key, required)
    return LEVELS[level] >= LEVELS[required]
