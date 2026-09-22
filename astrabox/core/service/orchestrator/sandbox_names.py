"""Where a sandbox's name goes when its destruction could not be confirmed.

The disposal judgement (:mod:`astrabox.seams.sandbox_disposal`) says whether a
last name may be severed. This module is the other half: it says where the name
goes when it may not be.

Every row that points at a sandbox has exactly one pointer field, and the
pointer is not free to stay put — a session that is recovering, terminating or
rebuilding needs that field for the box it is moving to. So "keep the name"
cannot mean "leave the pointer alone", or recovery would be blocked by every
box it failed to destroy. It means: move the name off the pointer and onto a
ledger, a list on the same row of ids this deployment created, did not confirm
destroyed, and must still be able to address.

The ledger is append-only in practice and deliberately unbounded in shape: an
entry leaves it when something confirms that box is gone, and never because it
has been there a while. A cap would silently drop exactly the ids that had been
hardest to reclaim.

This module makes no judgement of its own. It turns one
:class:`~astrabox.seams.sandbox_disposal.SandboxDestruction` into row updates,
so the rule "a pointer is cleared only by a confirmed destruction of the box it
names" is written once and every caller spells it the same way.
"""

from __future__ import annotations

from typing import Any, Mapping

from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_RETAINED,
    SandboxDestruction,
    may_sever_last_name,
)

#: Row field holding ids this deployment created and could not confirm gone.
UNDESTROYED_SANDBOX_IDS = "undestroyed_sandbox_ids"


def read_undestroyed(row: Mapping[str, Any] | None) -> list[str]:
    """The row's ledger, normalised (missing/malformed reads as empty)."""
    raw = (row or {}).get(UNDESTROYED_SANDBOX_IDS)
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(item or "").strip() for item in raw if str(item or "").strip()})


def keep_name_updates(
    destruction: SandboxDestruction | None,
    *,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Row updates that keep an unconfirmed sandbox addressable, or ``{}``.

    ``{}`` for a confirmed destruction and for a call that had no sandbox to
    act on — in neither case is there a surviving box to name. Otherwise the id
    is appended to the ledger, idempotently: the same failed destruction
    retried does not grow the list.
    """
    if destruction is None:
        return {}
    leaked = destruction.leaked_sandbox_id
    if leaked is None:
        return {}
    known = read_undestroyed(row)
    if leaked in known:
        return {}
    return {UNDESTROYED_SANDBOX_IDS: sorted({*known, leaked})}


def release_name_updates(
    destruction: SandboxDestruction | None,
    *,
    sandbox_id: str | None,
    row: Mapping[str, Any] | None = None,
    pointer_field: str = "sandbox_id",
    also_clear: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Row updates for "this box is gone, stop pointing at it" — or for "it is not".

    One call covers both directions of the pairing rule, so the two halves
    cannot drift apart:

    * the destruction confirms this exact sandbox — clear the pointer (and the
      fields that only mean anything alongside it), and drop the id from the
      ledger if it was ever on it;
    * the destruction RETAINED the box — clear the pointer too, and do not put
      the id on the ledger. The box is deliberately alive under a longer-lived
      owner that names it, so this caller's pointer was never the last one, and
      keeping it leaves an ended conversation claiming a box it has no part of.
    * anything else — leave the pointer alone and put the id on the ledger.
      The caller may still move the pointer itself for its own reasons; what it
      may not do is let the id stop existing.

    ``sandbox_id`` is passed separately from the verdict on purpose: a
    confirmed destruction of A does not license releasing a pointer that now
    names B (see :func:`~astrabox.seams.sandbox_disposal.may_sever_last_name`).
    """
    target = str(sandbox_id or "").strip()
    # `may_sever_last_name` asks whether this is the LAST name, and under
    # RETAINED it is not: the box's owner still names it, which is the whole
    # reason the box was left running. So RETAINED is licensed here without
    # loosening that predicate for anyone else.
    retained = bool(target) and (
        destruction is not None
        and destruction.outcome == SANDBOX_DESTRUCTION_RETAINED
        and destruction.sandbox_id == target
    )
    if not retained and not may_sever_last_name(destruction, sandbox_id=target):
        return keep_name_updates(destruction, row=row)
    updates: dict[str, Any] = {pointer_field: None}
    for field in also_clear:
        updates[field] = None
    known = read_undestroyed(row)
    if target in known:
        updates[UNDESTROYED_SANDBOX_IDS] = [item for item in known if item != target]
    return updates


__all__ = [
    "UNDESTROYED_SANDBOX_IDS",
    "keep_name_updates",
    "read_undestroyed",
    "release_name_updates",
]
