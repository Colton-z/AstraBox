"""Read the supplier's event suffix through one fixed follow-opening cut."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
)

_HISTORY_PAGE_MESSAGES = 100


async def read_history_events(
    call: Callable[..., Awaitable[Any]], *, address: dict[str, Any],
    through_seq: int, after_seq: int,
) -> list[dict[str, Any]]:
    """Page backwards at the native snapshot cut, returning the unseen suffix.

    Child histories and the main conversation use the same supplier protocol.
    A cached child opening may precede events already received on its stream;
    that older cut contributes no new records and cannot move its cursor back.
    """
    if after_seq >= through_seq:
        return []
    before_seq: int | None = None
    entries_by_seq: dict[int, dict[str, Any]] = {}
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
        page_seqs: list[int] = []
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
            page_seqs.append(seq)
            previous = entries_by_seq.get(seq)
            if previous is not None and previous != event:
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness session/page reused an event seq"
                )
            entries_by_seq[seq] = event
        if not value["hasMore"] or (page_seqs and min(page_seqs) <= after_seq):
            break
        if not page_seqs:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page cannot advance an empty page"
            )
        next_before = min(page_seqs)
        if before_seq is not None and next_before >= before_seq:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness session/page pagination did not advance"
            )
        before_seq = next_before
    return [entries_by_seq[seq] for seq in sorted(entries_by_seq) if seq > after_seq]
