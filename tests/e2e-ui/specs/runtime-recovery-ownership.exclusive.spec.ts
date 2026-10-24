import { execFileSync } from 'node:child_process';
import { expect, test as base, type APIRequestContext, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { configuredRecoveryReplicas, servicePython, type RecoveryReplica } from '../fixtures/recoveryReplicas';
import { PlatformApi } from '../fixtures/platformApi';
import { killSandbox, requireSandboxHandle, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

const test = base.extend<{}, { recoveryReplicas: { directory: string; children: RecoveryReplica[] } }>({
  recoveryReplicas: [async ({}, use) => {
    await use(configuredRecoveryReplicas());
  }, { scope: 'worker' }],
});

const sessions = trackSessions();
const agents: string[] = [];
const assistants: string[] = [];
onPassOnly(async ({ request }) => {
  for (const id of agents.splice(0)) await new AstraApi(request).deleteAgent(id);
  for (const id of assistants.splice(0)) await new AstraApi(request).deleteAssistant(id);
});

/** The session row's box binding and the claim of the recovery driving it. */
interface SessionBinding {
  sandbox_id: string | null;
  workspace_id: string | null;
  runtime_recovery_owner: string | null;
}

function sessionBinding(sessionId: string): SessionBinding | null {
  return postgresRows<SessionBinding>(
    `SELECT sandbox_id, workspace_id, runtime_recovery_owner FROM sessions `
      + `WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

/**
 * Clear the session's binding to `sandboxId`, the shape a lost process leaves:
 * the row names no box. Compare-and-set on the observed box, so a binding some
 * other writer replaced is not cleared; the ids of the rows it changed.
 */
function clearBinding(sessionId: string, sandboxId: string): string[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET sandbox_id = NULL, expires_at = NULL `
      + `WHERE session_id = ${sqlLiteral(sessionId)} AND sandbox_id = ${sqlLiteral(sandboxId)} `
      + `RETURNING session_id`,
  );
}

async function start(api: AstraApi) {
  return test.step('Create a cold Agent Session and reach READY before recovery', async () => {
    const configured = await api.defaultAgent();
    const model = String(configured.model || '').trim();
    const runtimeProfile = String(process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '').trim();
    expect(model, 'the deployed Agent declares this case model route').not.toBe('');
    expect(runtimeProfile, 'the deployment declares its cold RuntimeProfile').not.toBe('');
    const agent = await api.createAgent({
      name: `__e2e_runtime_recovery_${Date.now()}`, model,
      runtime_profile_name: runtimeProfile, prepared_capacity: 0,
    });
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
  expect(clearBinding(sessionId, sandboxId), 'the fault must clear exactly the observed binding')
    .toEqual([sessionId]);
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
  expect(recovered).not.toHaveProperty('runtime_recovery_owner');
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

test('independent recovery replicas converge before the first created runtime publishes its binding', async ({ page, request, recoveryReplicas }) => {
  const api = new AstraApi(request);
  const sessionId = await start(api);
  const marker = `REPLICA_MEMORY_${Date.now()}`;
  const first = await remember(api, page, sessionId, marker);
  const dead = await clearAndKill(api, sessionId);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const { directory, children } = recoveryReplicas;
  const python = (program: string, ...args: string[]) => servicePython(server, program, ...args);
  const read = (file: string, location = directory): string => python('import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else "")', `${location}/${file}`).trim();
  const release = (file: string) => python('import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', `${directory}/${file}`);
  const replica = (mode: string, location = directory, assistantId = '') => {
    const child = children.find((candidate) => candidate.mode === mode);
    if (!child || child.location !== location) throw new Error(`replica ${mode} was not configured for this location`);
    python('import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
      `${location}/${mode}.request.json`, JSON.stringify({ session_id: sessionId, assistant_id: assistantId }));
    return child;
  };
  const recoveryOwner = () => sessionBinding(sessionId)?.runtime_recovery_owner ?? null;
  const interruptedStartups = async () => {
    // The startup runs in a separate real process, and the serving replica's
    // periodic watcher was already bootstrapped before this Session existed.
    // Neither scenario writes Session state or substitutes the clock/provider.
    await Promise.all(['before-allocation', 'after-allocation', 'assistant-before-allocation'].map(async (mode) => {
      await test.step(`An interrupted ${mode} startup settles through the periodic sweep`, async () => {
        const location = `${directory}/${mode}`;
        let assistantId = '';
        if (mode === 'assistant-before-allocation') {
          const profile = await api.assistantEnvironmentName();
          const assistant = await api.createAssistant({
            display_name: `__e2e_orphan_materializer_${Date.now()}`,
            runtime_profile_name: profile,
            model_config_override: { model_name: await api.assistantModelName(profile) },
          });
          assistantId = assistant.assistant_id;
          assistants.push(assistantId);
        }
        const child = replica(mode, location, assistantId);
        await expect.poll(() => {
          if (child.process.exitCode !== null) throw new Error(`${mode} exited ${child.process.exitCode}: ${child.output.slice(-4000)}`);
          return read('accepted.json', location);
        }, { timeout: 30_000 }).not.toBe('');
        const accepted = JSON.parse(read('accepted.json', location)) as {
          session_id: string; grace_seconds: number; scan_interval_seconds: number;
        };
        const sid = accepted.session_id;
        sessions.push(sid);
        expect(accepted.grace_seconds, 'this candidate declares a short real startup grace').toBeGreaterThan(0);
        expect(accepted.grace_seconds).toBeLessThanOrEqual(10);
        expect(accepted.scan_interval_seconds).toBeLessThanOrEqual(2);
        const gate = mode === 'after-allocation' ? 'created.json' : 'before-create.json';
        await expect.poll(() => read(gate, location), { timeout: 90_000 }).not.toBe('');
        const rows = () => postgresRows<{
          state: string; sandbox_id: string | null; startup_allocation: { sandbox_id: string } | null;
          runtime_recovery_owner: string | null; last_error: string | null; elapsed: number;
        }>(`SELECT state, sandbox_id, startup_allocation, runtime_recovery_owner, last_error, `
          + `EXTRACT(EPOCH FROM clock_timestamp()-GREATEST(updated_at,created_at)) AS elapsed `
          + `FROM sessions WHERE session_id=${sqlLiteral(sid)}`)[0]!;
        const held = rows();
        expect(held.state).toBe('CREATING');
        expect(held.sandbox_id).toBeNull();
        expect(held.runtime_recovery_owner).toBeTruthy();
        if (mode !== 'after-allocation') expect(held.startup_allocation).toBeNull();
        const receipt = assistantId ? null : await api.postTurnInput(sid, `QUEUED_BEFORE_STARTUP_${sid}`);
        if (receipt) expect(receipt.state, 'holding a startup lock must not block accepted input custody').toBe('pending');
        let box: Awaited<ReturnType<typeof requireSandboxHandle>> | undefined;
        if (mode === 'after-allocation') {
          const native = JSON.parse(read('created.json', location)) as { sandbox_id: string };
          expect(held.startup_allocation?.sandbox_id).toBe(native.sandbox_id);
          box = await requireSandboxHandle(api, native.sandbox_id);
          // Age exceeds both grace and multiple watcher ticks, but an active
          // replica still owns this slow startup and no cleanup may kill it.
          await expect.poll(() => Number(rows().elapsed), { timeout: 30_000 })
            .toBeGreaterThan(accepted.grace_seconds + 2 * accepted.scan_interval_seconds);
          expect(rows().state).toBe('CREATING');
          expect((await api.getSession(sid)).state).toBe('CREATING');
        } else {
          expect(Number(rows().elapsed), 'kill the allocation-free owner inside its grace period')
            .toBeLessThan(accepted.grace_seconds);
        }
        python(`import os,pathlib,signal,sys
root=pathlib.Path(sys.argv[1]); mode=sys.argv[2]
pid=int((root/(mode+'.pid')).read_text())
parts=(pathlib.Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\\0')
if sys.argv[3].encode() not in parts or str(root).encode() not in parts:
    raise RuntimeError('startup PID does not belong to this fixture')
os.kill(pid,signal.SIGKILL)
`, location, mode, `${directory}/replica.py`);
        expect(await child.done, child.output.slice(-4000)).not.toBe(0);
        const ended = await api.waitForSessionState(sid, 'TERMINATED', 30_000);
        expect(ended.last_error).toContain('startup abandoned');
        expect(rows().state, 'terminal state must be durable, not a read-time age projection').toBe('TERMINATED');
        if (receipt) {
          await expect.poll(async () => (await api.getMessages(sid)).messages.find(
            (message) => (message.metadata as { input_id?: string } | undefined)?.input_id === receipt.input_id,
          )?.metadata, { timeout: 30_000 }).toMatchObject({ state: 'failed' });
        }
        if (assistantId) {
          expect((await api.getAssistant(assistantId)).workspace_state,
            'the actual failed Assistant materializer must not leave its owner MATERIALIZING')
            .toBe('HIBERNATING');
          const workspace = postgresRows<Record<string, unknown>>(
            `SELECT state, current_sandbox_id, provisioning_session_id, `
              + `provisioning_sandbox_generation, last_error FROM assistant_workspaces `
              + `WHERE assistant_id=${sqlLiteral(assistantId)}`,
          )[0];
          expect(workspace, 'an allocation-free failure releases the materializer claim and retains its cause')
            .toEqual({
              state: 'HIBERNATING', current_sandbox_id: null,
              provisioning_session_id: null, provisioning_sandbox_generation: null,
              last_error: 'materialization_startup_owner_exited',
            });
          await test.info().attach('assistant-startup-owner.json', {
            body: JSON.stringify({ assistantId, workspace }), contentType: 'application/json',
          });
        }
        if (box) {
          await waitForSandboxStopped(box, 30_000);
          await expect.poll(() => rows().startup_allocation, { timeout: 30_000 }).toBeNull();
        }
        const observer = await page.context().newPage();
        await openSessionView(observer, sid);
        await expect(observer.getByTestId('run-view')).toBeVisible();
        await expect(observer.getByText(String(ended.last_error), { exact: false })).toBeVisible();
        await observer.close();
        await test.info().attach(`startup-owner-${mode}.json`, {
          body: JSON.stringify({ accepted, held, ended, durable: rows(), receipt, native: box }),
          contentType: 'application/json',
        });
      });
    }));
  };
  const concurrentRecovery = async () => {
    const follower = replica('follower');
    await expect.poll(() => {
      if (follower.process.exitCode !== null) throw new Error(`follower exited ${follower.process.exitCode}: ${follower.output.slice(-4000)}`);
      return read('admitted');
    }, { timeout: 60_000 }).not.toBe('');
    const winner = replica('winner');
    await expect.poll(() => {
      if (winner.process.exitCode !== null) throw new Error(`winner exited ${winner.process.exitCode}: ${winner.output.slice(-4000)}`);
      return read('created.json');
    }, { timeout: 90_000 }).not.toBe('');
    const winnerId = (JSON.parse(read('created.json')) as { sandbox_id: string }).sandbox_id;
    expect(winnerId).not.toBe(dead);
    expect(sessionBinding(sessionId)?.sandbox_id || null, 'the gate must hold a genuinely unpublished binding').toBeNull();
    const claim = recoveryOwner();
    expect(claim, 'the winner holds the recovery claim').toBeTruthy();
    release('recover');
    await expect.poll(() => read('follower.json'), { timeout: 60_000 }).not.toBe('');
    expect(await follower.done, follower.output.slice(-4000)).toBe(0);
    expect(JSON.parse(read('follower.json'))).toEqual({ created: [] });
    expect(recoveryOwner(), 'both admitted requests share one recovery').toBe(claim);
    const concurrent = await api.recoverSession(sessionId);
    expect(concurrent).not.toHaveProperty('runtime_recovery_owner');
    expect(concurrent).not.toHaveProperty('retained_startup_allocations');
    expect(recoveryOwner(), 'a recovery in flight is not claimed again').toBe(claim);
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
    await api.archiveSession(sessionId);
  };
  try {
    await Promise.all([interruptedStartups(), concurrentRecovery()]);
  } finally {
    release('recover'); release('publish');
    for (const [index, child] of children.entries()) {
      await test.info().attach(`recovery-replica-${index}.log`, { body: child.output, contentType: 'text/plain' });
    }
  }
});

test('temporary-workspace recovery claims its own replenished pool and preserves the native conversation', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const environment = String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim();
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(environment, 'the deployed conversation-tenancy pool RuntimeProfile is required').not.toBe('');
  expect(researchAgent, 'the deployed model route is required').not.toBe('');
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const volume = execFileSync('docker', ['exec', server, 'python', '-c',
    'from astrabox.common.utils.settings import load_astrabox_settings; print(load_astrabox_settings().sandbox_workspace_volume or "")',
  ], { encoding: 'utf8', timeout: 20_000 }).trim();
  expect(volume, 'this is the no-volume recovery contract, not a volume replacement').toBe('');
  const model = await api.configuredAgentModel(researchAgent, environment);
  const name = `__e2e_runtime_recovery_same_display_prefix_${Date.now()}`;
  const agent = await api.createAgent({ name: `${name}_A`, model, runtime_profile_name: environment, prepared_capacity: 1 });
  agents.push(agent.agent_id);
  const sibling = await api.createAgent({ name: `${name}_B`, model, runtime_profile_name: environment, prepared_capacity: 1 });
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
  const oldWorkspace = sessionBinding(sessionId)?.workspace_id;
  expect(oldWorkspace).toBeTruthy();
  const replacement = await prepared(agent.agent_id, String(initial.sandbox_id));
  await clearAndKill(api, sessionId);
  await api.recoverSession(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  expect(ready.sandbox_id, 'recovery must consume the exact waiting box, not create cold compute').toBe(replacement.sandbox_id);
  expect(sessionBinding(sessionId)?.workspace_id).toBe(oldWorkspace);
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
