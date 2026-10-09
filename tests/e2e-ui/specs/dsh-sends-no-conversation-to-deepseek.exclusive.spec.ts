/**
 * A DeepSeek Harness box uploads no conversation to DeepSeek.
 *
 * The harness ships two uploads on: its session-log contributor sends each
 * conversation's not-yet-accepted log with every official DeepSeek request,
 * gateways included, and its OpenTelemetry exporter sends a conversation when
 * the user leaves feedback. The evidence is the harness' own: the composition
 * it prints for the profile and overlay the box serves (`--dump-config`), and
 * the environment of the server process that is running.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/** The rows of a `--dump-config` print, each from its `- id:` line to the next. */
function rows(dump: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const block of dump.split(/\n(?=\s*- id: )/)) {
    const id = /- id: ['"]?([\w.-]+)/.exec(block)?.[1];
    if (id) found.set(id, block);
  }
  return found;
}

test('a DeepSeek Harness box uploads no conversation to DeepSeek', async ({ request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  agentId = (await api.createAgent({
    name: `__e2e_dsh_privacy_${randomUUID()}`,
    environment_name: profile.environment_name,
    model: profile.model,
    prepared_capacity: 0,
  })).agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandbox = await requireSandboxHandle(api, String(ready.sandbox_id || ''));

  const dump = sandboxExec(sandbox, [
    'runuser -u "${ASTRABOX_WORKLOAD_USER:-agent}" -- env HOME="/home/${ASTRABOX_WORKLOAD_USER:-agent}"',
    '/opt/dsh/node_modules/.bin/dsh --profile web --patch /opt/dsh/profile.patch.yml --dump-config 2>&1',
  ].join(' '), 90_000);
  const upload = rows(dump).get('session-log-deepseek');
  expect(upload, `the harness composes its session-log contributor:\n${dump.slice(0, 2000)}`).toBeTruthy();
  expect(upload).toMatch(/enabled:\s*false/);
  expect(dump).not.toMatch(/unmatched[^\n]*session-log-deepseek/i);

  const telemetry = sandboxExec(sandbox, [
    'pid=$(pgrep -f -- "--profile web" | head -n 1)',
    '[ -n "$pid" ] && tr "\\0" "\\n" < "/proc/$pid/environ" | grep "^DSH_TELEMETRY_MODE=" || true',
  ].join('\n'));
  expect(telemetry.trim(), 'the running harness has OpenTelemetry delivery off').toBe('DSH_TELEMETRY_MODE=DISABLED');
});
