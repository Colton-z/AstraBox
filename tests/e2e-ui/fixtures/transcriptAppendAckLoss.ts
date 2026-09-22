/** Fault the real image-owned spool's response without extracting its credential. */
import fs from 'node:fs';
import path from 'node:path';

import { IMAGE_RUNNER_LAUNCHER, imageRunnerRestartScript } from './runnerRestart';
import { sandboxExec, type SandboxHandle } from './sandboxOps';

export type AppendFaultMode = 'ack' | '502' | 'malformed';

export interface AppendFaultEvidence {
  mode: AppendFaultMode;
  session_id: string;
  stage?: string;
  append_id?: string;
  key?: Record<string, unknown>;
  entries?: Record<string, unknown>[];
  errors: string[];
  sdk_accepts: Array<{ elapsed_seconds: number; payload_sha256: string }>;
  attempts: Array<{
    append_id: string;
    request_body_sha256: string;
    payload_sha256: string;
    declared_payload_sha256: string;
    status?: number;
    response_code?: string;
    store_sequence?: number;
    injected_transient_http: boolean;
    injected_timeout_after_commit?: boolean;
    injected_malformed_ack?: boolean;
  }>;
}

export function transcriptAppendFault(sandbox: SandboxHandle, sessionId: string) {
  if (!/^[a-f0-9-]+$/.test(sessionId)) throw new Error('invalid fault Session identity');
  const root = `/tmp/astrabox-transcript-ack-${sessionId}`;
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const write = (file: string, value: string) => [
    `runuser -u "$ASTRABOX_WORKLOAD_USER" -- /usr/local/bin/python3.12 -c ${quote([
      'import base64; from pathlib import Path',
      `p=Path(${JSON.stringify(`${root}/${file}`)})`,
      `p.with_suffix('.tmp').write_bytes(base64.b64decode(${JSON.stringify(Buffer.from(value).toString('base64'))}))`,
      "p.with_suffix('.tmp').replace(p)",
    ].join('; '))}`,
  ].join('\n');
  return {
    install() {
      const source = fs.readFileSync(path.join(__dirname, 'transcript_append_ack_runner.py'), 'utf8');
      return sandboxExec(sandbox, [
        'set -eu',
        `test ! -e ${quote(root)}`,
        `install -d -m 700 -o "$ASTRABOX_WORKLOAD_USER" ${quote(root)}`,
        write('runner.py', source),
        imageRunnerRestartScript({
          launch: `env ASTRABOX_INBOX_SERVER=${quote(`${root}/runner.py`)} ${IMAGE_RUNNER_LAUNCHER}`,
          log: `${root}/runner.log`,
          evidence: 'printf \'old_pid=%s replacement=transcript-response-fault\\n\' "$old_pid"',
          name: 'transcript response fault runner',
        }),
      ].join('\n'), 30_000).trim();
    },
    arm(mode: AppendFaultMode, marker: string) {
      sandboxExec(sandbox, write('arm.json', JSON.stringify({ mode, marker, session_id: sessionId })));
    },
    read(mode: AppendFaultMode): AppendFaultEvidence | null {
      const raw = sandboxExec(sandbox,
        `if test -f ${quote(`${root}/${mode}.json`)}; then cat ${quote(`${root}/${mode}.json`)}; else echo null; fi`);
      return JSON.parse(raw) as AppendFaultEvidence | null;
    },
    release(mode: AppendFaultMode) { sandboxExec(sandbox, write(`${mode}.release`, 'release')); },
    disarm() { sandboxExec(sandbox, write('disarmed', 'disarmed')); },
    pending(appendId: string) {
      return JSON.parse(sandboxExec(sandbox, `/usr/local/bin/python3.12 -c ${quote([
        'import json; from pathlib import Path',
        `target=${JSON.stringify(appendId)}`,
        "print(json.dumps([str(p) for p in Path('/tmp/astrabox-runner-spool').glob('*.batch.json') if json.loads(p.read_text())['append_id']==target]))",
      ].join('; '))}`)) as string[];
    },
  };
}
