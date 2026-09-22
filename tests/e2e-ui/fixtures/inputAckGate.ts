/**
 * Withhold one runner `input_ack` receipt inside a spec-owned cold sandbox.
 *
 * The receipt window this fixture opens is the platform's own: `RunnerLink.deliver()`
 * (`astrabox/core/service/orchestrator/engine/runner_link.py`) sends an `input`
 * frame and waits for the correlated `input_ack` the runner journals after
 * `RunnerSession.submit()` accepted the input. Nothing in the platform records
 * delivery before that receipt — `input.delivered` is journaled by
 * `JournalDeliveryOutbox.mark_delivered` only once `deliver()` returned — so a
 * receipt held on the wire is exactly "the engine accepted the input and the host
 * cannot know it".
 *
 * The mechanism is the same shape as the runner-restart fixtures: the image-baked
 * runner is relaunched through its own launcher on a loopback port, and
 * `input_ack_gate_proxy.py` takes the runner's public port. It forwards every
 * frame verbatim and answers `GET /health` from the runner, so the host's
 * readiness gate, `prepare`/`activate`/`attach`, journal replay and inputs all
 * reach the real runner unchanged. Only the armed Session's first `input_ack`
 * (and its replay on a reattach) is held until `releaseInputAckGate`.
 *
 * Whole-box placement is required: the runner process is replaced. Use the
 * spec's own conversation-tenancy Agent (`AstraApi.createColdTestAgent`).
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import {
  IMAGE_RUNNER_LAUNCHER,
  IMAGE_RUNNER_PORT,
  IMAGE_RUNNER_PYTHON,
  imageRunnerRestartScript,
} from './runnerRestart';
import { sandboxExec, type SandboxHandle } from './sandboxOps';

export const INPUT_ACK_GATE_PROXY_PATH = '/tmp/astrabox-e2e-input-ack-gate-proxy.py';
export const INPUT_ACK_GATE_STATE_PATH = '/tmp/astrabox-e2e-input-ack-gate-state.json';
export const INPUT_ACK_GATE_RELEASE_PATH = '/tmp/astrabox-e2e-input-ack-gate.release';
export const INPUT_ACK_GATE_LOG_PATH = '/tmp/astrabox-e2e-input-ack-gate.log';

const PROXY_SOURCE_PATH = join(__dirname, 'input_ack_gate_proxy.py');

export interface InputAckGateInputObservation {
  command_id: string;
  input_id: string;
  sequence: number | null;
  observed_at: number;
}

export interface InputAckGateEntered extends InputAckGateInputObservation {
  session_id: string;
  ack_seq: number | null;
  ack_duplicate: boolean | null;
  connection_no: number;
  entered_at: number;
}

export interface InputAckGateState {
  session_id: string;
  state: 'armed' | 'entered' | 'released' | 'timed_out' | string;
  max_wait_seconds: number;
  proxy_pid?: number;
  connections?: number;
  holds?: number;
  inputs?: InputAckGateInputObservation[];
  target?: InputAckGateInputObservation;
  entered?: InputAckGateEntered;
  released_at?: number;
  timed_out_at?: number;
  wire_events?: Record<string, unknown>[];
  errors?: string[];
}

export interface InputAckGateInstallEvidence {
  oldRunnerPid: number;
  proxyPid: number;
  upstreamPort: number;
  state: InputAckGateState;
  restartEvidence: string;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * Relaunch the runner behind the gate and arm it for one Session.
 *
 * The image launcher honours `ASTRABOX_RUNNER_PORT`, so the relaunched runner is
 * the image's own process on a kernel-selected free port; the proxy is started
 * in the same detached session and answers `IMAGE_RUNNER_PORT`. The restart
 * script's `/health` gate now passes only when both are up, because the proxy
 * answers that probe from the runner. The runner's in-memory slot is gone with
 * the old process; the caller evicts the host runtime so the next turn rebuilds
 * its link through the gate.
 */
export function installInputAckGate(
  sandbox: SandboxHandle,
  options: { sessionId: string; maxWaitSeconds: number },
): InputAckGateInstallEvidence {
  const sessionId = String(options.sessionId || '').trim();
  if (!sessionId) throw new Error('inputAckGate: sessionId is required');
  const maxWaitSeconds = Number(options.maxWaitSeconds);
  if (!Number.isFinite(maxWaitSeconds) || maxWaitSeconds <= 0) {
    throw new Error('inputAckGate: maxWaitSeconds must be a positive number');
  }
  // Ask this sandbox's network namespace, not the host or a guessed static port.
  // This is a preflight, not a reservation: the real bind/health must still pass.
  const portProgram = [
    'import socket',
    'with socket.socket() as listener:',
    '    listener.bind(("0.0.0.0", 0))',
    '    print(listener.getsockname()[1])',
  ].join('\n');
  const upstreamPort = Number(sandboxExec(
    sandbox,
    `${IMAGE_RUNNER_PYTHON} -c ${shellQuote(portProgram)}`,
    15_000,
  ).trim());
  if (!Number.isInteger(upstreamPort) || upstreamPort <= 0 || upstreamPort > 65535
    || upstreamPort === IMAGE_RUNNER_PORT) {
    throw new Error(`inputAckGate: invalid allocated upstream port ${upstreamPort}`);
  }
  const source = readFileSync(PROXY_SOURCE_PATH, 'utf8');
  const initialState: InputAckGateState = {
    session_id: sessionId,
    state: 'armed',
    max_wait_seconds: maxWaitSeconds,
    holds: 0,
    inputs: [],
  };
  const proxyEnv = [
    `ASTRABOX_E2E_INPUT_ACK_GATE_STATE=${INPUT_ACK_GATE_STATE_PATH}`,
    `ASTRABOX_E2E_INPUT_ACK_GATE_RELEASE=${INPUT_ACK_GATE_RELEASE_PATH}`,
    `ASTRABOX_E2E_INPUT_ACK_GATE_PORT=${IMAGE_RUNNER_PORT}`,
    `ASTRABOX_E2E_INPUT_ACK_GATE_UPSTREAM_PORT=${upstreamPort}`,
  ].join(' ');
  // One detached session holds both: the image launcher (relaunched on the
  // loopback port) in the background and the proxy as the foreground process.
  const launch =
    `sh -c 'ASTRABOX_RUNNER_PORT=${upstreamPort} ${IMAGE_RUNNER_LAUNCHER} & `
    + `exec env ${proxyEnv} ${IMAGE_RUNNER_PYTHON} ${INPUT_ACK_GATE_PROXY_PATH}'`;
  const script = [
    'set -eu',
    `rm -f ${INPUT_ACK_GATE_RELEASE_PATH}`,
    `printf '%s' ${shellQuote(JSON.stringify(initialState))} > ${INPUT_ACK_GATE_STATE_PATH}`,
    `base64 -d > ${INPUT_ACK_GATE_PROXY_PATH} <<'ASTRABOX_E2E_PROXY'`,
    Buffer.from(source, 'utf8').toString('base64'),
    'ASTRABOX_E2E_PROXY',
    `${IMAGE_RUNNER_PYTHON} -c ${shellQuote(`import ast; ast.parse(open(${JSON.stringify(INPUT_ACK_GATE_PROXY_PATH)}).read())`)}`,
    `${IMAGE_RUNNER_PYTHON} -c "import websockets"`,
    imageRunnerRestartScript({
      launch,
      log: INPUT_ACK_GATE_LOG_PATH,
      evidence: `printf 'old_pid=%s proxy_port=%s upstream_port=%s\\n' "$old_pid" '${IMAGE_RUNNER_PORT}' '${upstreamPort}'`,
      name: 'input-ack gated runner',
    }),
  ].join('\n');
  const restartEvidence = sandboxExec(sandbox, script, 60_000).trim();
  const oldPidMatch = /old_pid=(\d+) /.exec(restartEvidence);
  if (!oldPidMatch) {
    throw new Error(`inputAckGate: restart did not report the replaced runner pid: ${restartEvidence}`);
  }
  // The proxy's health answer came from the runner on the loopback port; prove
  // that runner answers its own port directly too, so the gate is in front of
  // the image's runner rather than a stranger.
  const upstreamHealth = sandboxExec(
    sandbox,
    `${IMAGE_RUNNER_PYTHON} -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:${upstreamPort}/health', timeout=2).read().decode())"`,
  ).trim();
  if (upstreamHealth !== 'OK') {
    throw new Error(`inputAckGate: relaunched runner did not answer /health on :${upstreamPort}: ${upstreamHealth}`);
  }
  const state = readInputAckGate(sandbox);
  if (state.state !== 'armed' || !Number.isInteger(state.proxy_pid) || Number(state.proxy_pid) <= 1) {
    throw new Error(`inputAckGate: proxy did not arm its state: ${JSON.stringify(state)}`);
  }
  return {
    oldRunnerPid: Number(oldPidMatch[1]),
    proxyPid: Number(state.proxy_pid),
    upstreamPort,
    state,
    restartEvidence,
  };
}

/** The gate's current state, as the proxy last wrote it. */
export function readInputAckGate(sandbox: SandboxHandle): InputAckGateState {
  const raw = sandboxExec(sandbox, `cat ${INPUT_ACK_GATE_STATE_PATH}`, 15_000).trim();
  if (!raw) throw new Error('inputAckGate: state file is empty');
  return JSON.parse(raw) as InputAckGateState;
}

/**
 * Wait until the armed Session's first `input_ack` is being held.
 *
 * `entered` carries the wire facts the spec correlates with the platform: the
 * command id and the platform input id from the `input` frame, plus the runner
 * sequence of the receipt now withheld.
 */
export async function waitForInputAckGateEntered(
  sandbox: SandboxHandle,
  sessionId: string,
  timeoutMs: number,
): Promise<InputAckGateState> {
  const deadline = Date.now() + timeoutMs;
  let last: InputAckGateState | null = null;
  while (Date.now() < deadline) {
    last = readInputAckGate(sandbox);
    if (last.state === 'timed_out') break;
    if (
      last.state === 'entered'
      && last.entered
      && last.entered.session_id === sessionId
      && last.entered.command_id
      && last.entered.input_id
    ) {
      return last;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `inputAckGate: receipt for ${sessionId} was not held within ${timeoutMs}ms; last=${JSON.stringify(last)}`,
  );
}

/** Release on the next connection too, even while the platform is stopped. */
export function signalInputAckGateRelease(sandbox: SandboxHandle): void {
  sandboxExec(sandbox, `: > ${INPUT_ACK_GATE_RELEASE_PATH}`, 15_000);
}

/** Create the release marker and wait for the proxy to acknowledge it. */
export async function releaseInputAckGate(
  sandbox: SandboxHandle,
  timeoutMs = 15_000,
): Promise<InputAckGateState> {
  signalInputAckGateRelease(sandbox);
  const deadline = Date.now() + timeoutMs;
  let last: InputAckGateState | null = null;
  while (Date.now() < deadline) {
    last = readInputAckGate(sandbox);
    if (last.state === 'released') return last;
    if (last.state === 'timed_out') break;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`inputAckGate: proxy did not release within ${timeoutMs}ms; last=${JSON.stringify(last)}`);
}

/** Tail of the proxy/runner log for failure evidence. */
export function inputAckGateLogTail(sandbox: SandboxHandle, lines = 60): string {
  return sandboxExec(sandbox, `tail -${lines} ${INPUT_ACK_GATE_LOG_PATH} || true`, 15_000).trim();
}
