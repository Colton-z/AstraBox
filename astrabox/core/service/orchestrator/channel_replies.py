"""What a channel delivers of a Session's output: one reply per message segment.

A response's browser parts in the Session's output log form one or more
message segments. Each opens with a ``start`` part and closes with a
``finish``. A response that waits on a person closes its segment after the
interaction's ``data-interaction`` part and reopens the same message with
``start`` once it continues (the runtime contract,
``astrabox/core/service/orchestrator/engine/runtime.py``). A channel shows each
segment as one reply:

* the first segment's reply id is the response id; a later one is
  ``<response_id>~<interaction_id>``, named after the interaction that closed
  the segment before it;
* a reply's text is its text parts folded the way the page folds them
  (:func:`~astrabox.core.service.orchestrator.session_runtime.ui_message.fold_message`),
  separate text parts as paragraphs;
* a reply that closed at an interaction names it, so its delivery can show the
  question while the response still waits on it.

Parts that arrive while no segment is open (a response the runtime lost after
its last interaction closes with ``error`` and ``finish`` and no new
``start``) belong to no reply.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from astrabox.core.service.orchestrator.session_runtime.ui_message import fold_message
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

#: Parts that open or close a reply. A committed batch without either changes
#: no reply's boundaries.
REPLY_BOUNDARY_PARTS = frozenset({"start", "finish"})

#: How many output rows one read takes from the log.
_OUTPUT_PAGE = 500


@dataclass(frozen=True, slots=True)
class ChannelReply:
    """One message segment of a response, as recorded so far."""

    reply_id: str
    response_id: str
    #: The output sequence of the segment's ``start`` part.
    first_seq: int
    #: The output sequence of the segment's last recorded part.
    last_seq: int
    #: The segment's ``finish`` is recorded.
    complete: bool
    text: str
    #: The interaction the segment closed at, while the response waits on a person.
    interaction_id: str | None
    #: What the response's ``error`` part says, when it failed in this segment.
    error: str | None


def segment_reply(
    reply_id: str, response_id: str, rows: Sequence[Mapping[str, Any]]
) -> ChannelReply:
    """The reply one segment's rows form, from its ``start`` through its ``finish`` when recorded."""
    if not rows:
        raise ValueError(f"reply {reply_id} has no recorded parts")
    parts = [row["part"] for row in rows]
    folded = fold_message(reply_id, parts)
    text = "\n\n".join(
        str(part["text"])
        for part in folded["parts"]
        if part.get("type") == "text" and part.get("text")
    )
    interaction_id = next(
        (
            str((part.get("data") or {}).get("interaction_id") or "") or None
            for part in parts
            if part.get("type") == "data-interaction"
        ),
        None,
    )
    error = next(
        (
            str(part.get("errorText") or "") or "the response failed"
            for part in parts
            if part.get("type") == "error"
        ),
        None,
    )
    return ChannelReply(
        reply_id=reply_id,
        response_id=response_id,
        first_seq=int(rows[0]["seq"]),
        last_seq=int(rows[-1]["seq"]),
        complete=parts[-1].get("type") == "finish",
        text=text,
        interaction_id=interaction_id,
        error=error,
    )


def reply_segments(response_id: str, rows: Iterable[Mapping[str, Any]]) -> list[ChannelReply]:
    """The replies one response's parts form, in order.

    ``rows`` are the response's output rows (``seq``, ``part``) in sequence
    order, from its first part. Raises ``ValueError`` when a ``start`` reopens
    a response that no interaction paused: the runtime contract reopens a
    message only after one, so such a log has no reply identity to give.
    """
    replies: list[ChannelReply] = []
    segment: list[Mapping[str, Any]] | None = None
    reply_id = ""
    last_interaction: str | None = None
    for row in rows:
        part = row["part"]
        kind = part.get("type")
        if kind == "data-interaction":
            last_interaction = str((part.get("data") or {}).get("interaction_id") or "") or None
        if segment is None:
            if kind != "start":
                continue
            if replies:
                if last_interaction is None:
                    raise ValueError(
                        f"response {response_id} reopened at output {row['seq']} "
                        "without an interaction"
                    )
                reply_id = f"{response_id}~{last_interaction}"
            else:
                reply_id = response_id
            segment = [row]
            continue
        segment.append(row)
        if kind == "finish":
            replies.append(segment_reply(reply_id, response_id, segment))
            segment = None
    if segment is not None:
        replies.append(segment_reply(reply_id, response_id, segment))
    return replies


class ReplyReader:
    """Reads one reply from the Session's output log as the runtime's parts are recorded.

    Each :meth:`read` takes only the rows recorded since the previous one, so a
    delivery that follows a long reply reads every part once.
    """

    def __init__(
        self,
        facts: SessionRuntimeRepository,
        session_id: str,
        *,
        reply_id: str,
        first_seq: int,
    ) -> None:
        self._facts = facts
        self._session_id = session_id
        self._reply_id = reply_id
        self._first_seq = first_seq
        self._after = first_seq - 1
        self._response_id: str | None = None
        self._rows: list[Mapping[str, Any]] = []
        self._complete = False

    async def read(self) -> ChannelReply:
        """The reply as recorded so far.

        Raises ``RuntimeError`` when the log holds no ``start`` at the reply's
        first sequence: the delivery row names a reply the log does not have.
        """
        while not self._complete:
            rows = await self._facts.read_output(
                self._session_id, after_seq=self._after, limit=_OUTPUT_PAGE
            )
            for row in rows:
                self._after = int(row["seq"])
                if self._response_id is None:
                    if int(row["seq"]) != self._first_seq or row["part"].get("type") != "start":
                        raise RuntimeError(
                            f"session {self._session_id} output {self._first_seq} "
                            f"does not start reply {self._reply_id}"
                        )
                    self._response_id = str(row["response_id"] or "")
                elif row.get("response_id") != self._response_id:
                    continue
                self._rows.append(row)
                if row["part"].get("type") == "finish":
                    self._complete = True
                    break
            if len(rows) < _OUTPUT_PAGE:
                break
        if self._response_id is None:
            raise RuntimeError(
                f"session {self._session_id} has no output {self._first_seq} "
                f"for reply {self._reply_id}"
            )
        return segment_reply(self._reply_id, self._response_id, self._rows)


__all__ = [
    "REPLY_BOUNDARY_PARTS",
    "ChannelReply",
    "ReplyReader",
    "reply_segments",
    "segment_reply",
]
