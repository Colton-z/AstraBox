/**
 * Arm and release the backend's `turn_frame_processed` barrier from a spec.
 *
 * The barrier (`astrabox/testing/e2e_faults.py`, registered only when
 * `ASTRABOX_E2E_FAULTS` is armed) pauses a real turn worker immediately after
 * it has processed one matching translated frame, and holds it until this
 * declaration says `release`. That is what lets a spec keep "the reply is still
 * owed" true for a bounded, deterministic window instead of racing the model.
 *
 * The declaration channel is a file the backend watches: the base path plus its
 * `.d/*.json` siblings, one file per worker so parallel specs never share a
 * declaration. The write is atomic (temp file + rename) because the backend
 * reads it on its own schedule and must never see half a JSON document.
 *
 * `_FRAME_HOLD_RELEASE_TIMEOUT_SECONDS` is 120 on the backend side: a hold that
 * is never released raises there and fails the turn for a reason that has
 * nothing to do with the spec. Every caller must fit its whole outage inside
 * that window and release in a `finally`.
 */
import fs from 'node:fs';
import path from 'node:path';

import { test } from '@playwright/test';

/** The deployment's declaration channel; `compose.e2e.yaml` shares it host↔container. */
export const FRAME_HOLD_FAULT_BASE = (
  process.env.ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE
  || '/tmp/astrabox-e2e-turn-terminal-drop-faults.json'
).trim();

export const FRAME_HOLD_FAULT_DIR = `${FRAME_HOLD_FAULT_BASE}.d`;

export interface FrameHoldFault {
  faults: { hold_after_frame: number };
  match: { session_id: string; frame_type: string };
  release: boolean;
  consumed: unknown[];
}

/**
 * A declaration path nobody else in this run can collide with.
 *
 * Worker index and pid, because two workers of the same lane run the same file
 * names; the test title and session id, because a reader who finds the file
 * afterwards needs to know which case left it behind.
 */
export function frameHoldFaultPath(sessionId: string): string {
  const slug = `${test.info().title} ${sessionId}`
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-|-$/g, '');
  return path.join(FRAME_HOLD_FAULT_DIR, `w${test.info().workerIndex}-${process.pid}-${slug}.json`);
}

function writeFrameHoldFault(faultPath: string, payload: FrameHoldFault): void {
  const temporary = `${faultPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(payload), 'utf8');
  fs.chmodSync(temporary, 0o644);
  fs.renameSync(temporary, faultPath);
}

/**
 * Declare: hold this session's turn after its first *frameType* frame.
 *
 * The directory is made world-writable because the backend container runs as a
 * different uid than the runner and writes `consumed` back into this same file;
 * `mkdirSync`'s mode argument is masked by the process umask, so the chmod is
 * separate on purpose.
 */
export function armFrameHoldFault(
  faultPath: string,
  sessionId: string,
  frameType: string,
): void {
  fs.mkdirSync(FRAME_HOLD_FAULT_DIR, { recursive: true });
  fs.chmodSync(FRAME_HOLD_FAULT_DIR, 0o777);
  writeFrameHoldFault(faultPath, {
    faults: { hold_after_frame: 1 },
    match: { session_id: sessionId, frame_type: frameType },
    release: false,
    consumed: [],
  });
}

export function readFrameHoldFault(faultPath: string): FrameHoldFault | null {
  if (!faultPath || !fs.existsSync(faultPath)) return null;
  try {
    return JSON.parse(fs.readFileSync(faultPath, 'utf8')) as FrameHoldFault;
  } catch {
    // A torn read races the backend's own write-back; the caller polls.
    return null;
  }
}

/**
 * Has the backend consumed THIS session's declaration for *frameType*?
 *
 * The anti-vacuity oracle. Without it every assertion downstream could pass on
 * an ordinary turn that the barrier never touched — including on a deployment
 * where `ASTRABOX_E2E_FAULTS` is not armed at all and the hook is not installed.
 */
export function frameHoldConsumed(
  faultPath: string,
  sessionId: string,
  frameType: string,
): boolean {
  const consumed = readFrameHoldFault(faultPath)?.consumed;
  if (!Array.isArray(consumed)) return false;
  return consumed.some((entry) => {
    const record = entry as { session_id?: unknown; frame_type?: unknown };
    return record.session_id === sessionId && record.frame_type === frameType;
  });
}

/** Let the held worker go. Idempotent; a missing declaration is not an error. */
export function releaseFrameHoldFault(faultPath: string): void {
  const payload = readFrameHoldFault(faultPath);
  if (!payload || payload.release) return;
  writeFrameHoldFault(faultPath, { ...payload, release: true });
}

/** Remove the declaration so no later turn in this deployment inherits it. */
export function clearFrameHoldFault(faultPath: string): void {
  if (faultPath && fs.existsSync(faultPath)) fs.rmSync(faultPath, { force: true });
}
