"""The Agent's MCP assignment list: which catalog entries, from which provider.

This is the one record that says what MCP servers an Agent may use. It stores a
provider name beside each id rather than bare ids, so no catalog is the implied
one and a deployment can assign from several at once — the bundled gateway, the
registry AstraBox stores itself, or a provider the deployment installed.

Each surface edits only its own provider's rows: the administrator API owns the
built-in ones, an extension console owns the ones from the catalog it is
showing, and neither disturbs the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: The Agent-document field holding ``[{"provider": ..., "item_id": ...}]``.
AGENT_MCP_ASSIGNMENTS_FIELD = "mcp_assignments"


def normalize_assignments(raw: Any) -> list[tuple[str, str]]:
    """Read the stored list as ``(provider, item_id)`` pairs, in order.

    Malformed rows are dropped rather than raised: this reads a stored
    document, and one unusable row must not make an Agent unresolvable.
    """
    if not isinstance(raw, list):
        return []
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip().lower()
        item_id = str(entry.get("item_id") or "").strip()
        if not provider or not item_id or (provider, item_id) in seen:
            continue
        seen.add((provider, item_id))
        result.append((provider, item_id))
    return result


def assignments_for_provider(raw: Any, provider: str) -> list[str]:
    """The catalog ids assigned to this Agent from one provider, in order."""
    wanted = str(provider or "").strip().lower()
    return [
        item_id
        for provider_name, item_id in normalize_assignments(raw)
        if provider_name == wanted
    ]


def replace_provider_assignments(
    raw: Any, provider: str, item_ids: Sequence[str]
) -> list[dict[str, str]]:
    """Return the list with ``provider``'s rows replaced by ``item_ids``.

    Rows from every other provider are preserved in their original order and
    kept ahead of the new ones, so saving one console's selection never drops
    an assignment made somewhere else.
    """
    wanted = str(provider or "").strip().lower()
    retained = [
        {"provider": provider_name, "item_id": item_id}
        for provider_name, item_id in normalize_assignments(raw)
        if provider_name != wanted
    ]
    written: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in item_ids:
        item_id = str(value or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        written.append({"provider": wanted, "item_id": item_id})
    return [*retained, *written]


__all__ = [
    "AGENT_MCP_ASSIGNMENTS_FIELD",
    "assignments_for_provider",
    "normalize_assignments",
    "replace_provider_assignments",
]
