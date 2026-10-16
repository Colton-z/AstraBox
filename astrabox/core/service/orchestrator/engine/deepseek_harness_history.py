"""Read the supplier's durable Session log backwards from one fixed cut.

``session/page`` returns contiguous pages of a Session log ending at an
inclusive ``throughSeq`` (the cut a ``session/follow`` snapshot reports), each
page reaching further back through ``beforeSeq``
(``@deepseek-ai/dsh-api-session-controller`` ``SessionPageRequest``). Every
durable event carries its own ``seq``, and the log is append-only, so the
events through one cut are the same whenever they are read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
)

_HISTORY_PAGE_MESSAGES = 100

DshCall = Callable[..., Awaitable[Any]]


async def _pages_backward(
    call: DshCall, *, address: dict[str, Any], through_seq: int,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Each page's events in ascending order, newest page first, until the log's start."""
    before_seq: int | None = None
    seen: dict[int, dict[str, Any]] = {}
    while True:
        payload: dict[str, Any] = {
            "address": address, "throughSeq": through_seq,
            "maxMessages": _HISTORY_PAGE_MESSAGES,
        }
        if before_seq is not None:
            payload["beforeSeq"] = before_seq
        value = await call("session/page", {"args": {"request": payload}})
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page returned no events list"
            )
        if not isinstance(value.get("hasMore"), bool):
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page returned no hasMore flag"
            )
        page: list[dict[str, Any]] = []
        for raw_entry in value["records"]:
            if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("event"), dict):
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness session/page returned a malformed entry"
                )
            event = dict(raw_entry["event"])
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness session/page event lacks a non-negative seq"
                )
            previous = seen.get(seq)
            if previous is not None and previous != event:
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness session/page reused an event seq"
                )
            seen[seq] = event
            page.append(event)
        page.sort(key=lambda event: event["seq"])
        yield page
        if not value["hasMore"]:
            return
        if not page:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page cannot advance an empty page"
            )
        next_before = page[0]["seq"]
        if before_seq is not None and next_before >= before_seq:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page pagination did not advance"
            )
        before_seq = next_before


async def read_history_events(
    call: DshCall, *, address: dict[str, Any], through_seq: int, after_seq: int,
) -> list[dict[str, Any]]:
    """The durable events after ``after_seq`` through ``through_seq``, in order.

    Child histories and the main conversation use the same supplier protocol.
    A cached child opening may precede events already received on its stream;
    that older cut contributes no new records and cannot move its cursor back.
    The supplier keeps sequence numbers contiguous, so a suffix with a hole is
    refused rather than returned short.
    """
    if after_seq >= through_seq:
        return []
    events: dict[int, dict[str, Any]] = {}
    async for page in _pages_backward(call, address=address, through_seq=through_seq):
        for event in page:
            events[event["seq"]] = event
        if page and page[0]["seq"] <= after_seq:
            break
    suffix = [events[seq] for seq in sorted(events) if seq > after_seq]
    if [event["seq"] for event in suffix] != list(range(after_seq + 1, through_seq + 1)):
        raise DeepSeekHarnessProtocolError(
            "deepseek_harness session/page history is not a contiguous event suffix"
        )
    return suffix


async def open_turn_events(
    call: DshCall, *, address: dict[str, Any], through_seq: int,
) -> list[dict[str, Any]]:
    """The open turn's events through ``through_seq``, starting at its ``turn/start``.

    Empty when the log's last turn boundary is a ``turn/end`` or the log has
    no turn: the harness opens a turn with ``turn/start`` and closes it with
    exactly one ``turn/end`` (``@deepseek-ai/dsh-session`` ``SessionEventMap``).
    """
    if through_seq < 0:
        return []
    collected: list[dict[str, Any]] = []
    async for page in _pages_backward(call, address=address, through_seq=through_seq):
        # Older pages go in front, so this page starts the collected events.
        collected = page + collected
        for index in range(len(page) - 1, -1, -1):
            kind = page[index].get("type")
            if kind == "turn/end":
                return []
            if kind == "turn/start":
                return collected[index:]
    return []
