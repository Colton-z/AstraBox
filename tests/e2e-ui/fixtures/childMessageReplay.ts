/** Replay retained child frames through the real journal, without mocking HTTP or the renderer. */
import { execFileSync } from 'node:child_process';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

export function appendChildFrames(sessionId: string, payloads: Record<string, unknown>[]): void {
  execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', `
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.persistence.repository.session_event_repository import SessionEventRepository
from astrabox.common.utils.time_utils import utcnow_iso
async def main():
    request = json.load(sys.stdin)
    repo = SessionEventRepository()
    first = await repo.allocate_event_sequence(request['session_id'], count=len(request['payloads']))
    await repo.append_frames([
        {'session_id': request['session_id'], 'frame_seq': first + index,
         'scope': 'session', 'turn_id': None, 'created_at': utcnow_iso(),
         'engine_kind': payload['data']['engineKind'], 'payload': payload}
        for index, payload in enumerate(request['payloads'])
    ])
asyncio.run(main())
`,
  ], { input: JSON.stringify({ session_id: sessionId, payloads }), encoding: 'utf8', timeout: 30_000 });
}
