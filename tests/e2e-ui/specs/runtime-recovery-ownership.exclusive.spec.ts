import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { patchSessionDoc, sessionDoc, sessionEvents } from '../fixtures/dbOracle';
import { repoRoot } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { killSandbox, requireSandboxHandle, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
const agents: string[] = [];
onPassOnly(async ({ request }) => {
  for (const id of agents.splice(0)) await new AstraApi(request).deleteAgent(id);
});

async function start(api: AstraApi) {
  return test.step('Create a cold Agent Session and reach READY before recovery', async () => {
    const agent = await api.createColdTestAgent(`__e2e_runtime_recovery_${Date.now()}`);
    agents.push(agent.agent_id);
    const created = await api.startConversation(agent.agent_id);
    sessions.push(created.session_id);
    await api.waitForSessionReady(created.session_id);
    return created.session_id;
  });
}

async function clearAndKill(api: AstraApi, sessionId: string) {
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '');
  expect(sandboxId).not.toBe('');
  const handle = await requireSandboxHandle(api, sandboxId);
  const original = patchSessionDoc(sessionId, { sandbox_id: null, sandbox_endpoint: null, expires_at: null });
  expect(original).toHaveLength(1);
  expect(original[0].sandbox_id).toBe(sandboxId);
  killSandbox(handle);
  await waitForSandboxStopped(handle, 30_000);
  return sandboxId;
}

async function remember(api: AstraApi, page: Page, sessionId: string, marker: string) {
  const turn = await api.sendTurn(sessionId,
    `The marker value for this conversation is ${marker}. Reply with exactly that value, `
      + 'without Markdown or any other words. Do not use tools.', 90_000);
  expect(turn.errorText).toBeNull();
  expect(turn.text.trim(), 'the initial reply must confirm the value, not merely mention its name').toBe(marker);
  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);
  const text = page.getByTestId('assistant-message').last().getByTestId('assistant-text');
  // The virtualized list stays visibility:hidden until it has scrolled to its
  // first position, so its text content is present before anything is shown.
  // Wait on the rendered text, not the DOM text, before taking the baseline.
  await expect(text).toHaveText(marker, { useInnerText: true });
  const rendered = await text.innerText();
  await test.info().attach('initial-recovery-memory.json', {
    body: JSON.stringify({ sessionId, raw: turn.text, rendered }), contentType: 'application/json',
  });
  await page.goto('about:blank', { waitUntil: 'domcontentloaded', timeout: 5_000 });
  return rendered;
}

async function explicitRecovery(page: Page, request: APIRequestContext, fault: 'live' | 'bound' | 'cleared') {
  const api = new AstraApi(request);
  const sessionId = await start(api);
  const marker = `RECOVERY_MEMORY_${Date.now()}`;
  const first = await remember(api, page, sessionId, marker);
  const original = await api.waitForSessionReady(sessionId);
  if (fault === 'cleared') {
    await clearAndKill(api, sessionId);
  } else if (fault === 'bound') {
    const handle = await requireSandboxHandle(api, String(original.sandbox_id));
    killSandbox(handle);
    await waitForSandboxStopped(handle, 30_000);
  }
  await api.recoverSession(sessionId);
  const recovered = await api.waitForSessionReady(sessionId);
  expect(recovered.sandbox_id).toBeTruthy();
  if (fault === 'live') expect(recovered.sandbox_id).toBe(original.sandbox_id);
  else expect(recovered.sandbox_id).not.toBe(original.sandbox_id);
  expect(recovered).not.toHaveProperty('_runtime_recovery_owner');
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('assistant-message').first().getByTestId('assistant-text')).toHaveText(first);
  const before = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, 'Repeat the marker value from the beginning of this conversation. Reply with exactly that value, without Markdown or any other words. Do not use tools.');
  const reply = await api.waitForAssistantMessageMatching(sessionId, before, (m) => messageText(m).trim() === marker, 90_000);
  expect(messageText(reply).trim()).toBe(marker);
  await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')).toHaveText(marker);
  expect((await api.waitForSessionReady(sessionId)).sandbox_id).toBe(recovered.sandbox_id);
}

test('explicit recovery reattaches live compute and keeps browser history', async ({ page, request }) => {
  await explicitRecovery(page, request, 'live');
});

test('explicit recovery replaces dead bound compute and keeps browser history', async ({ page, request }) => {
  await explicitRecovery(page, request, 'bound');
});

test('explicit recovery replaces a cleared dead cache and keeps browser history', async ({ page, request }) => {
  await explicitRecovery(page, request, 'cleared');
});

test('independent recovery replicas converge before the first created runtime publishes its binding', async ({ page, request }) => {
  const api = new AstraApi(request);
  const sessionId = await start(api);
  const marker = `REPLICA_MEMORY_${Date.now()}`;
  const first = await remember(api, page, sessionId, marker);
  const dead = await clearAndKill(api, sessionId);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const directory = `/tmp/astrabox-recovery-${sessionId}`;
  const python = (program: string, ...args: string[]) => execFileSync('docker', [
    'exec', server, 'python', '-c', program, ...args,
  ], { encoding: 'utf8', timeout: 20_000 });
  python('import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir()', directory);
  execFileSync('docker', ['cp', path.join(repoRoot, 'tests/e2e-ui/fixtures/runtime_recovery_replica.py'), `${server}:${directory}/replica.py`], { timeout: 20_000 });
  const read = (file: string): string => python('import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else "")', `${directory}/${file}`).trim();
  const release = (file: string) => python('import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', `${directory}/${file}`);
  const children: { process: ChildProcess; output: string; done: Promise<number | null> }[] = [];
  const settledWithin = async (done: Promise<number | null>, milliseconds: number) => {
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([
        done.then(() => true), new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(false), milliseconds); }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  };
  const replica = (mode: string) => {
    const process = spawn('docker', ['exec', server, 'python', `${directory}/replica.py`, '--session-id', sessionId, '--directory', directory, '--mode', mode]);
    const item = { process, output: '', done: new Promise<number | null>((resolve, reject) => {
      process.on('error', reject); process.on('close', resolve);
    }) };
    process.stdout.on('data', (part) => { item.output += part.toString(); });
    process.stderr.on('data', (part) => { item.output += part.toString(); });
    children.push(item);
    return item;
  };
  const startupCount = () => sessionEvents(sessionId).filter((event) => (
    event.event_type === 'command.accepted'
    && (event.payload as Record<string, unknown>)?.command_type === 'StartSessionStartup'
  )).length;
  const beforeStarts = startupCount();
  try {
    const follower = replica('follower');
    await expect.poll(() => {
      if (follower.process.exitCode !== null) throw new Error(follower.output.slice(-4000));
      return read('admitted');
    }, { timeout: 60_000 }).not.toBe('');
    const winner = replica('winner');
    await expect.poll(() => {
      if (winner.process.exitCode !== null) throw new Error(winner.output.slice(-4000));
      return read('created.json');
    }, { timeout: 90_000 }).not.toBe('');
    const winnerId = (JSON.parse(read('created.json')) as { sandbox_id: string }).sandbox_id;
    expect(winnerId).not.toBe(dead);
    expect(sessionDoc(sessionId)?.sandbox_id || null, 'the gate must hold a genuinely unpublished binding').toBeNull();
    release('recover');
    await expect.poll(() => read('follower.json'), { timeout: 60_000 }).not.toBe('');
    expect(await follower.done, follower.output.slice(-4000)).toBe(0);
    expect(JSON.parse(read('follower.json'))).toEqual({ created: [] });
    expect(startupCount(), 'both admitted requests share one startup command').toBe(beforeStarts + 1);
    const concurrent = await api.recoverSession(sessionId);
    expect(concurrent).not.toHaveProperty('_runtime_recovery_owner');
    expect(concurrent).not.toHaveProperty('_retained_startup_allocations');
    expect(startupCount()).toBe(beforeStarts + 1);
    release('publish');
    await expect.poll(() => read('winner.json'), { timeout: 90_000 }).not.toBe('');
    expect(await winner.done, winner.output.slice(-4000)).toBe(0);
    expect(JSON.parse(read('winner.json'))).toEqual({ created: [winnerId] });
    expect((await api.waitForSessionReady(sessionId)).sandbox_id).toBe(winnerId);
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-message').first().getByTestId('assistant-text')).toHaveText(first);
    const before = await api.assistantCount(sessionId);
    await sendPrompt(page, sessionId, 'Repeat the marker value from the beginning of this conversation. Reply with exactly that value, without Markdown or any other words. Do not use tools.');
    const reply = await api.waitForAssistantMessageMatching(sessionId, before, (m) => messageText(m).trim() === marker, 90_000);
    expect(messageText(reply).trim()).toBe(marker);
    await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')).toHaveText(marker);
    expect((await api.waitForSessionReady(sessionId)).sandbox_id).toBe(winnerId);
  } finally {
    release('recover'); release('publish');
    for (const [index, child] of children.entries()) {
      const finished = await settledWithin(child.done, 10_000);
      if (!finished) {
        const mode = index === 0 ? 'follower' : 'winner';
        python(`import os,pathlib,signal,sys
root=pathlib.Path(sys.argv[1]); mode=sys.argv[2]
pid=int((root/(mode+'.pid')).read_text())
cmd=pathlib.Path('/proc')/str(pid)/'cmdline'
if cmd.exists():
    parts=cmd.read_bytes().split(b'\\0')
    if str(root/'replica.py').encode() not in parts: raise RuntimeError('replica PID no longer belongs to this probe')
    os.kill(pid, signal.SIGTERM)
`, directory, mode);
        if (!await settledWithin(child.done, 5_000)) {
          throw new Error(`owned ${mode} replica did not exit after verified SIGTERM: ${child.output.slice(-4000)}`);
        }
      }
      await test.info().attach(`recovery-replica-${index}.log`, { body: child.output, contentType: 'text/plain' });
    }
  }
});

test('temporary-workspace recovery claims its own replenished pool and preserves the native conversation', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const environment = String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim();
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(environment, 'the deployed conversation-tenancy pool Environment is required').not.toBe('');
  expect(researchAgent, 'the deployed model route is required').not.toBe('');
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const volume = execFileSync('docker', ['exec', server, 'python', '-c',
    'from astrabox.common.utils.settings import load_astrabox_settings; print(load_astrabox_settings().sandbox_workspace_volume or "")',
  ], { encoding: 'utf8', timeout: 20_000 }).trim();
  expect(volume, 'this is the no-volume recovery contract, not a volume replacement').toBe('');
  const model = await api.configuredAgentModel(researchAgent, environment);
  const name = `__e2e_runtime_recovery_same_display_prefix_${Date.now()}`;
  const agent = await api.createAgent({ name: `${name}_A`, model, environment_name: environment, prewarm_enabled: true });
  agents.push(agent.agent_id);
  const sibling = await api.createAgent({ name: `${name}_B`, model, environment_name: environment, prewarm_enabled: true });
  agents.push(sibling.agent_id);
  async function prepared(id: string, previous = '') {
    let status: Record<string, unknown> = {};
    await expect.poll(async () => {
      status = await platform.preparedRuntime(id);
      return status.ready === true && Number(status.prepared_count) > 0
        && Boolean(status.sandbox_id) && status.sandbox_id !== previous;
    }, { timeout: 90_000, message: 'the actual official pool must expose a borrowable box' }).toBe(true);
    return status;
  }
  const initial = await prepared(agent.agent_id);
  const other = await prepared(sibling.agent_id);
  expect(initial.client_pool_name).toBeTruthy();
  expect(other.client_pool_name).toBeTruthy();
  expect(initial.client_pool_name).not.toBe(other.client_pool_name);
  expect(initial.runtime_generation).not.toBe(other.runtime_generation);
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  expect((await api.waitForSessionReady(sessionId)).sandbox_id).toBe(initial.sandbox_id);
  const marker = `WARM_RECOVERY_${Date.now()}`;
  const first = await remember(api, page, sessionId, marker);
  const oldDetail = await api.adminSessionDetail(sessionId);
  const oldWorkspace = sessionDoc(sessionId)?.workspace_id;
  expect(oldWorkspace).toBeTruthy();
  const replacement = await prepared(agent.agent_id, String(initial.sandbox_id));
  await clearAndKill(api, sessionId);
  await api.recoverSession(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  expect(ready.sandbox_id, 'recovery must consume the exact waiting box, not create cold compute').toBe(replacement.sandbox_id);
  expect(sessionDoc(sessionId)?.workspace_id).toBe(oldWorkspace);
  const detail = await api.adminSessionDetail(sessionId);
  for (const field of ['linux_user', 'home_dir', 'workspace_dir']) {
    const previous = (oldDetail.runtime_identity as Record<string, unknown>)[field];
    expect(previous, `baseline ${field} must be concrete`).toBeTruthy();
    expect((detail.runtime_identity as Record<string, unknown>)[field]).toBe(previous);
  }
  expect(detail.engine_session_key).toBe(oldDetail.engine_session_key);
  const otherAfter = await prepared(sibling.agent_id);
  expect(otherAfter.sandbox_id, 'another Agent pool must remain untouched').toBe(other.sandbox_id);
  expect(otherAfter.client_pool_name).toBe(other.client_pool_name);
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('assistant-message').first().getByTestId('assistant-text')).toHaveText(first);
  const before = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, 'Repeat the marker value from the beginning of this conversation. Reply with exactly that value, without Markdown or any other words. Do not use tools.');
  const reply = await api.waitForAssistantMessageMatching(sessionId, before, (m) => messageText(m).trim() === marker, 90_000);
  expect(messageText(reply).trim()).toBe(marker);
  await expect(page.getByTestId('assistant-message').last().getByTestId('assistant-text')).toHaveText(marker);
  expect((await api.waitForSessionReady(sessionId)).sandbox_id).toBe(replacement.sandbox_id);
});
