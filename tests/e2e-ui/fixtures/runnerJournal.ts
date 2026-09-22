/** Observation for the existing real runner-restart/compaction fault. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { IMAGE_RUNNER_PATH, IMAGE_RUNNER_PORT, IMAGE_RUNNER_PYTHON } from './runnerRestart';
import { sandboxExec, type SandboxHandle } from './sandboxOps';

const PROBE_PATH = '/tmp/astrabox-e2e-runner-journal-probe.py';
const EVIDENCE_PATH = '/tmp/astrabox-e2e-runner-journal.jsonl';

export interface RunnerFrame extends Record<string, unknown> {
  op: string;
  seq?: number;
  message_type?: string;
  message?: Record<string, unknown>;
}

export interface JournalObservation {
  session_id: string;
  result_sequence: number;
  store_sequence: number;
  last_sequence: number;
  removed: number;
  before: RunnerFrame[];
  after: RunnerFrame[];
}

interface JournalBatch {
  after_sequence: number;
  hello: { session_id: string; last_seq: number; first_retained_sequence: number };
  frames: RunnerFrame[];
}

export interface RunnerJournalEvidence {
  observations: JournalObservation[];
  cold: JournalBatch;
  terminal: JournalBatch;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** Copy the observer only; the caller's existing restart helper launches it. */
export function observedJournalRunnerLaunch(sandbox: SandboxHandle, threshold: number): string {
  if (!Number.isSafeInteger(threshold) || threshold < 1) throw new Error('journal threshold must be positive');
  const source = readFileSync(join(__dirname, 'runner_journal_probe.py'), 'utf8');
  sandboxExec(sandbox, [
    'set -eu',
    `test ! -e ${PROBE_PATH} && test ! -e ${EVIDENCE_PATH}`,
    `printf '%s' ${shellQuote(source)} > ${PROBE_PATH}`,
  ].join('\n'), 15_000);
  return `${IMAGE_RUNNER_PYTHON} ${PROBE_PATH} run ${IMAGE_RUNNER_PATH} ${threshold} ${EVIDENCE_PATH}`;
}

/** Two real wire attaches, bounded by the existing get_init_info response. */
export function readCompactedRunnerJournal(sandbox: SandboxHandle, sessionId: string): RunnerJournalEvidence {
  if (!sessionId.trim()) throw new Error('journal probe requires its owning Session');
  return JSON.parse(sandboxExec(sandbox,
    `${IMAGE_RUNNER_PYTHON} ${PROBE_PATH} probe ${IMAGE_RUNNER_PORT} ${shellQuote(sessionId)} ${EVIDENCE_PATH}`,
    25_000)) as RunnerJournalEvidence;
}
