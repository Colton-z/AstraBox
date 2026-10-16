"""The file changes a conversation's responses made, for the Diff panel.

An engine adapter reports the files a tool call changed as a
``data-file-changes`` part (``engine/file_changes.py``); the session's reader
stores it in the output log with the response that made it. The Diff panel
reads them here rather than from the conversation it has loaded, so changes in
responses it never scrolled to still show.

A read names the output position it has already seen (``after_seq``) and gets
every response that ended after it, up to the position stored now
(``through_seq``), each with all of its file changes. A response comes back
whole, also when it has none, so a reader replaces what it held for that
response. A response still running is the follow stream's to show.
"""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

FILE_CHANGES_PART = "data-file-changes"


class FileChangeCursorError(ValueError):
    """``after_seq`` lies outside the session's output."""


async def file_changes(
    facts: SessionRuntimeRepository,
    reads: SessionReadRepository,
    session_id: str,
    *,
    after_seq: int,
) -> dict[str, Any]:
    """``{"through_seq", "responses": [{"response_id", "parts"}]}`` for the responses that ended after ``after_seq``."""
    through_seq, _cursor = await facts.resume_point(session_id)
    if after_seq < 0 or after_seq > through_seq:
        raise FileChangeCursorError("after_seq is outside this session's output")
    response_ids = await reads.responses_ended_between(session_id, after_seq=after_seq, through_seq=through_seq)
    parts: dict[str, list[dict[str, Any]]] = {response_id: [] for response_id in response_ids}
    for row in await reads.parts_of_type(
        session_id, FILE_CHANGES_PART, response_ids=response_ids, through_seq=through_seq
    ):
        parts[str(row["response_id"])].append(row["part"])
    return {
        "through_seq": through_seq,
        "responses": [{"response_id": response_id, "parts": parts[response_id]} for response_id in response_ids],
    }
