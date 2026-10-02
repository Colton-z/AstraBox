/** Hold the exact renewal through the existing gated fault declaration channel. */
import fs from 'node:fs';

import { FRAME_HOLD_FAULT_DIR, frameHoldFaultPath } from './frameHold';

interface RenewalHold {
  faults: { hold_prepared_slot_renewal: number };
  match: { agent_id: string; slot_id: string };
  release: boolean;
  consumed: Array<{ fault: string; agent_id: string; slot_id: string }>;
}

export function preparedRenewalHold(agentId: string, slotId: string) {
  const faultPath = frameHoldFaultPath(`${agentId}-${slotId}`);
  const write = (payload: RenewalHold) => {
    const temporary = `${faultPath}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(payload), 'utf8');
    fs.chmodSync(temporary, 0o644);
    fs.renameSync(temporary, faultPath);
  };
  const read = (): RenewalHold => JSON.parse(fs.readFileSync(faultPath, 'utf8'));
  fs.mkdirSync(FRAME_HOLD_FAULT_DIR, { recursive: true });
  fs.chmodSync(FRAME_HOLD_FAULT_DIR, 0o777);
  write({
    faults: { hold_prepared_slot_renewal: 1 },
    match: { agent_id: agentId, slot_id: slotId },
    release: false,
    consumed: [],
  });
  return {
    consumed: () => read().consumed.some((entry) => (
      entry.fault === 'hold_prepared_slot_renewal'
      && entry.agent_id === agentId && entry.slot_id === slotId
    )),
    evidence: read,
    release: () => write({ ...read(), release: true }),
    // Removing a declaration also releases a failed test's held worker.
    clear: () => fs.rmSync(faultPath, { force: true }),
  };
}
