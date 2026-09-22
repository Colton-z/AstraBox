"""Segment-opening rows carry the part-vocabulary contract they were written under.

Durable frames outlive the deployment that wrote them, so their grammar is an
external contract (docs/maintainers/seam-freeze-decisions-2026-08.md §1). The
repository stamps every ``start`` row at the door — one stamp per segment, all
writers included — and never rewrites a stamp a replayed historical row
already carries.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.persistence.repository import session_event_repository as repository_module
from astrabox.persistence.repository.session_event_repository import (
    SEGMENT_PART_CONTRACT,
    SessionEventRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.setattr(repository_module, "_index_ready", False)
    monkeypatch.setattr(repository_module, "_counter_index_ready", False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _frame(seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "command_id": "command-1",
        "frame_seq": seq,
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_start_rows_are_stamped_and_existing_stamps_survive() -> None:
    repository = SessionEventRepository()

    await repository.append_frames(
        [
            _frame(1, {"type": "start", "messageId": "turn-1"}),
            _frame(2, {"type": "text-delta", "id": "t0", "delta": "hello"}),
            _frame(
                3,
                {
                    "type": "start",
                    "messageId": "turn-2",
                    "messageMetadata": {
                        "turn_id": "turn-2",
                        "part_contract": "astrabox.parts/0",
                    },
                },
            ),
        ]
    )

    rows = {row["frame_seq"]: row["payload"] for row in await repository.list_frames("session-1")}

    # The segment opener gets the current contract at the door, whichever
    # writer appended it.
    assert rows[1]["messageMetadata"]["part_contract"] == SEGMENT_PART_CONTRACT
    # Rows inside a segment carry no stamp of their own: one per segment.
    assert "messageMetadata" not in rows[2]
    # A replayed historical segment keeps the contract it was written under;
    # restamping would falsify the one fact the field exists to preserve.
    assert rows[3]["messageMetadata"]["part_contract"] == "astrabox.parts/0"
    assert rows[3]["messageMetadata"]["turn_id"] == "turn-2"
