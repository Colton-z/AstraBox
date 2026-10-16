"""Session notices sent with ``NOTIFY`` by the statements that change a session.

A notice is sent when its statement's transaction commits, so a listener that
receives one can read the change it announces. Kinds: ``input`` (an input was
accepted), ``output`` (a reader committed output through ``seq``), ``answer``
(a person answered an interaction), ``control`` (a person asked to interrupt the runtime or stop a sub-agent),
``reader`` (the session's runtime became ready or was declared dead, so a
reader may be needed or must re-check its runtime), ``permission`` (the
session's permission mode changed and its runtime must follow), ``terminal``
(the session's web terminal recorded output through ``seq``, or started a
command when ``seq`` is 0), ``terminal_interrupt`` (a person asked the
terminal's running command to stop), ``session`` (what a page shows of the
session record changed, such as its generated title), ``transcript`` (native
SessionStore entries were committed).
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import ColumnElement, func, literal

CHANNEL = "astrabox_session"

NoticeKind = Literal[
    "input", "output", "reader", "answer", "control", "permission", "terminal", "terminal_interrupt", "session", "transcript"
]

_KINDS = (
    "input", "output", "reader", "answer", "control", "permission", "terminal", "terminal_interrupt", "session", "transcript"
)


def notice(
    session_id: ColumnElement[Any] | str,
    kind: NoticeKind,
    seq: ColumnElement[Any] | int = 0,
) -> ColumnElement[Any]:
    """A ``pg_notify`` expression; the payload is ``kind:seq:session_id``."""
    sid = session_id if isinstance(session_id, ColumnElement) else literal(session_id)
    position = seq if isinstance(seq, ColumnElement) else literal(seq)
    return func.pg_notify(CHANNEL, func.concat(kind, ":", position, ":", sid))


def parse_notice(payload: str) -> tuple[NoticeKind, int, str] | None:
    kind, _, rest = payload.partition(":")
    seq_text, _, session_id = rest.partition(":")
    if kind not in _KINDS or not session_id or not seq_text.isdigit():
        return None
    return kind, int(seq_text), session_id  # type: ignore[return-value]
