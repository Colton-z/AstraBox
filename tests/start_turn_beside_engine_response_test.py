"""Resident task launches are durable before their response ends."""

from __future__ import annotations

import uuid

import pytest

from astrabox.core.service.orchestrator.engine.platform_events import (
    PlatformResidentOutputSink,
)
from astrabox.persistence.repository import SessionEventRepository, SessionSnapshotRepository


@pytest.mark.asyncio
async def test_resident_launches_are_durable_before_terminal_and_replay_once() -> None:
    from astrabox.core.service.orchestrator.engine.emissions import emission_from_translated_frame

    session_id = str(uuid.uuid4())
    journal = SessionEventRepository()
    snapshots = SessionSnapshotRepository()
    await snapshots.apply_channel_update(
        session_id, channel='conversation', event_seq=1, updates={'conversation_state': 'IDLE'},
    )
    sink = PlatformResidentOutputSink(session_id, broker=None, journal_repo=journal, snapshots_repo=snapshots)
    handle = await sink.open_resident_response(
        engine_kind='claude_code', response_id='parent-response', engine_session_key='native-session',
        causation_id='native-boundary', native_message={'type': 'user', 'uuid': 'parent-response'}, runner_sequence=1,
    )
    assert handle is not None
    for manifest_id in ['launch-1', 'launch-2', 'launch-1']:
        await sink.publish_resident_output(handle, [emission_from_translated_frame({
            'type': 'background-tasks-opened', 'manifest_id': manifest_id,
            'manifest': {'engine_refs': [manifest_id]},
        })], engine_sequence_number=2)
    rows = await journal.list_events(session_id, after_seq=0, limit=100)
    opened = [row for row in rows if row['event_type'] == 'turn.background_tasks_opened']
    assert [row['payload']['engine_refs'] for row in opened] == [['launch-1'], ['launch-2']]
    assert not any(row['event_type'] in {'turn.completed', 'turn.failed'} for row in rows)
    assert (await snapshots.get_snapshot(session_id))['current_turn_id'] == 'parent-response'
