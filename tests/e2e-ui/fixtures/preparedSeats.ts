/**
 * The prepared seats of an Agent-shared Agent, read where the product keeps them.
 *
 * A pool box arrives at its Agent carrying its seats. Until a conversation takes
 * the box, the seats are listed only in a receipt inside it; once the Agent holds
 * the box, the ones still waiting are in the Agent row's `_prepared_slots`
 * ledger. A claim takes the oldest prepared seat of the ledger, else of the pool
 * box it takes next, so that is the seat these helpers name as next.
 *
 * Seats also carry credentials; only public runtime coordinates leave the
 * reading process.
 */
import { execFileSync } from 'node:child_process';

import { documentsByField } from './dbOracle';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

export interface PreparedSeat extends Record<string, unknown> {
  slot_id: string;
  state: string;
  placement: string;
  sandbox_id: string;
  isolated_session_id: string;
  terminal_isolated_session_id: string;
  home_dir: string;
  prepared_at: string;
  runtime_generation: string;
}

const PUBLIC_KEYS = [
  'slot_id', 'state', 'placement', 'sandbox_id', 'isolated_session_id',
  'terminal_isolated_session_id', 'home_dir', 'prepared_at', 'runtime_generation',
  'claimed_session_id',
] as const;

function project(seat: Record<string, unknown>): PreparedSeat {
  return Object.fromEntries(PUBLIC_KEYS.map((key) => [key, seat[key]])) as PreparedSeat;
}

/** Every seat in an Agent row's ledger, in publication order. */
export function seatsInRow(agent: Record<string, unknown>): PreparedSeat[] {
  const seats = Array.isArray(agent._prepared_slots) ? agent._prepared_slots : [];
  return seats.map((seat) => project(seat as Record<string, unknown>));
}

/** Every seat in the Agent's ledger, in publication order. */
export function ledgerSeats(agentId: string): PreparedSeat[] {
  return seatsInRow(documentsByField('agents', '$.agent_id', agentId)[0] || {});
}

/** The oldest seat still waiting for a conversation. */
export function oldestPrepared(seats: PreparedSeat[]): PreparedSeat | undefined {
  return seats
    .filter((seat) => seat.state === 'prepared')
    .sort((a, b) => String(a.prepared_at).localeCompare(String(b.prepared_at)))[0];
}

/** The ledger seat the Agent's next conversation claims. */
export function nextLedgerSeat(agentId: string): PreparedSeat | undefined {
  return oldestPrepared(ledgerSeats(agentId));
}

/** The seats prepared in a pool box the Agent has not taken yet. */
export function poolBoxSeats(agentId: string, poolBox: string): PreparedSeat[] {
  const output = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', `
import asyncio, json, sys
from astrabox.bootstrap import bootstrap
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.sandbox import sandbox_for_name
from astrabox.core.service.orchestrator.agent.prepared_slots import POOL_SEATS_RECEIPT
KEYS = ${JSON.stringify(PUBLIC_KEYS)}
async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    row = await AgentRepository().get_agent(sys.argv[1]) or {}
    handle = await sandbox_for_name(row['_client_pool_backend']).connect(sys.argv[2])
    seats = json.loads(await handle.files.read_file(POOL_SEATS_RECEIPT))
    print(json.dumps([{key: seat.get(key) for key in KEYS} for seat in seats]))
asyncio.run(main())
`, agentId, poolBox,
  ], { encoding: 'utf8', timeout: 30_000 });
  return (JSON.parse(output) as Record<string, unknown>[]).map(project);
}

/**
 * The seat the Agent's next conversation claims: the oldest prepared seat of
 * its ledger, else of the pool box the prepared-runtime status names.
 */
export function nextPreparedSeat(agentId: string, statusSandboxId: string): PreparedSeat | undefined {
  return nextLedgerSeat(agentId) || oldestPrepared(poolBoxSeats(agentId, statusSandboxId));
}
