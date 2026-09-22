/** Drop only later partial events from a real dedicated Claude SDK runner. */
import fs from 'node:fs';
import path from 'node:path';

import { IMAGE_RUNNER_LAUNCHER, imageRunnerRestartScript } from './runnerRestart';
import { sandboxExec, type SandboxHandle } from './sandboxOps';

export interface CompleteMessageEvidence {
  stage: string;
  first_tool_message_id: string;
  forwarded_text: string;
  suppressed_events: number;
  suppressed_message_ids: string[];
  complete_messages: Array<{ message_id: string; text: string; tool_ids: string[]; error: string | null }>;
  errors: string[];
  result_is_error?: boolean;
}

export function sdkCompleteMessageFault(sandbox: SandboxHandle, sessionId: string) {
  if (!/^[a-f0-9-]+$/.test(sessionId)) throw new Error('invalid fault Session identity');
  const root = `/tmp/astrabox-sdk-complete-${sessionId}`;
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const write = (file: string, content: string) =>
    `runuser -u "$ASTRABOX_WORKLOAD_USER" -- /usr/local/bin/python3.12 -c ${quote([
      'import base64; from pathlib import Path',
      `p=Path(${JSON.stringify(`${root}/${file}`)})`,
      `p.write_bytes(base64.b64decode(${JSON.stringify(Buffer.from(content).toString('base64'))}))`,
    ].join('; '))}`;
  return {
    install() {
      const source = fs.readFileSync(path.join(__dirname, 'sdk_complete_message_runner.py'), 'utf8');
      return sandboxExec(sandbox, [
        'set -eu', `test ! -e ${quote(root)}`,
        `install -d -m 700 -o "$ASTRABOX_WORKLOAD_USER" ${quote(root)}`,
        write('runner.py', source),
        imageRunnerRestartScript({
          launch: `env ASTRABOX_INBOX_SERVER=${quote(`${root}/runner.py`)} ${IMAGE_RUNNER_LAUNCHER}`,
          log: `${root}/runner.log`,
          evidence: 'printf \'old_pid=%s replacement=sdk-complete-message-fault\\n\' "$old_pid"',
          name: 'SDK complete message fault runner',
        }),
      ].join('\n'), 30_000).trim();
    },
    read(): CompleteMessageEvidence | null {
      return JSON.parse(sandboxExec(sandbox,
        `if test -f ${quote(`${root}/evidence.json`)}; then cat ${quote(`${root}/evidence.json`)}; else echo null; fi`));
    },
    release() { sandboxExec(sandbox, write('release', 'release')); },
    disarm() { sandboxExec(sandbox, write('disarmed', 'disarmed')); },
  };
}
