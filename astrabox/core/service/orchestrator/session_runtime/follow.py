"""A page's view of a session's output: what it missed, then what happens next.

:func:`follow_output` yields the session's browser parts after a sequence, in
order and once each, from the output log and then live. Live parts come
straight from a reader in this process; a reader in another process is heard
through its commit notices and read back from the log. Each yielded burst ends
with a ``data-resume-cursor`` part carrying the last sequence, which the page
sends back as ``after_seq`` when it reconnects. A change to what the page shows
of the session record (startup state or its generated title) arrives as a transient
``data-session-changed`` part, after which the page reads the session again.

One call ends after the part that finishes a response, so each HTTP response
carries one UI message; the page reconnects for the next.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from astrabox.core.service.orchestrator.session_runtime.feed import (
    LivePart,
    Notice,
    Resync,
    SessionFeed,
)
from astrabox.core.service.orchestrator.session_runtime.ui_message import public_part
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

_PAGE = 500
#: Live parts held while the log has not yet caught up to them.
_HELD_LIMIT = 5000


def resume_cursor_part(seq: int) -> dict[str, Any]:
    return {"type": "data-resume-cursor", "transient": True, "data": {"seq": seq}}


SESSION_CHANGED_PART: dict[str, Any] = {"type": "data-session-changed", "transient": True, "data": {}}
TRANSCRIPT_CHANGED_PART: dict[str, Any] = {"type": "data-transcript-changed", "transient": True, "data": {}}


async def follow_output(
    feed: SessionFeed, repo: SessionRuntimeRepository, session_id: str, *, after_seq: int
) -> AsyncIterator[dict[str, Any]]:
    with feed.subscribe(session_id) as subscription:
        last = after_seq
        held: dict[int, dict[str, Any]] = {}
        # Subscribe before asking the page to reread: startup can finish between
        # its initial detail read and this connection, without any output.
        yield dict(SESSION_CHANGED_PART)
        yield dict(TRANSCRIPT_CHANGED_PART)

        def emit(seq: int, stored: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal last
            last = seq
            part = public_part(stored, shared=False)
            if part is None:
                return []
            if part.get("type") == "error":
                # The AI SDK stops reading at an error part, so its cursor goes first.
                return [resume_cursor_part(seq), part]
            return [part]

        def finishes(part: dict[str, Any]) -> bool:
            return part.get("type") in ("finish", "error")

        async def from_log() -> AsyncIterator[dict[str, Any]]:
            while True:
                rows = await repo.read_output(session_id, after_seq=last, limit=_PAGE)
                for row in rows:
                    for out in emit(int(row["seq"]), row["part"]):
                        yield out
                    if finishes(row["part"]):
                        return
                if len(rows) < _PAGE:
                    return

        async for out in from_log():
            yield out
            if out.get("type") in ("finish", "error"):
                yield resume_cursor_part(last)
                return
        if last != after_seq:
            yield resume_cursor_part(last)

        while True:
            item = await subscription.get()
            burst: list[dict[str, Any]] = []
            ended = False
            if isinstance(item, Notice) and item.kind in ("session", "reader"):
                burst.append(dict(SESSION_CHANGED_PART))
            elif isinstance(item, Notice) and item.kind == "transcript":
                burst.append(dict(TRANSCRIPT_CHANGED_PART))
            elif isinstance(item, LivePart):
                if item.seq > last and len(held) < _HELD_LIMIT:
                    held[item.seq] = item.part
            elif (isinstance(item, Notice) and item.kind == "output" and item.seq > last) or isinstance(
                item, Resync
            ):
                if isinstance(item, Resync):
                    burst.append(dict(SESSION_CHANGED_PART))
                    burst.append(dict(TRANSCRIPT_CHANGED_PART))
                async for out in from_log():
                    burst.append(out)
                    if out.get("type") in ("finish", "error"):
                        ended = True
                        break
            while not ended and last + 1 in held:
                part = held.pop(last + 1)
                burst.extend(emit(last + 1, part))
                ended = finishes(part)
            for seq in [seq for seq in held if seq <= last]:
                held.pop(seq)
            if not burst:
                continue
            for out in burst:
                yield out
            if burst[-1].get("type") != "data-resume-cursor":
                yield resume_cursor_part(last)
            if ended:
                return
