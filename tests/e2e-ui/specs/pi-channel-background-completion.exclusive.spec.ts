/** Pi main-agent output reaches its channel after a native child finishes. */
import { createHmac, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { documentsByField, framesForTurn, replaceDocs, sessionEvents } from '../fixtures/dbOracle';
import { engineProfileFor } from '../fixtures/engineProfile';
import { absoluteBaseUrl, apiPath } from '../fixtures/env';
import { InstalledChannelBackend } from '../fixtures/installedChannelBackend';
import { childPrompt, expectNativeMode, launchEvidence, nativeRows } from '../fixtures/nativeChildLifecycle';
import { finalReplies, nativeEntries, nativeText } from '../fixtures/piChildFailure';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { PlatformApi } from '../fixtures/platformApi';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

const cases = [
  { restart: false, installed: false, title: 'Pi sends its native background completion to the channel without another input' },
  { restart: true, installed: false, title: 'Pi delivers its native background completion after a server restart without another input' },
  { restart: true, installed: true, title: 'an installed channel package receives native and Web replies across backend restarts' },
];

for (const scenario of cases) test(scenario.title, async ({ request, page }, info) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('pi');
  const recipient = await channelCallback();
  const id = randomUUID();
  let sessionId = '';
  let stoppedServer = '';
  let installed: InstalledChannelBackend | undefined;
  let completed = false;
  let rootScope: unknown;
  const evidence: Record<string, unknown> = {};
  try {
    if (scenario.installed) {
      expect((await platform.listChannelProviders()).filter((row) => row.name === 'installed_probe')).toEqual([]);
      installed = new InstalledChannelBackend(`astrabox-installed-channel-${id}`, info.outputPath('installed-channel.json'));
      evidence.installedPackage = installed.evidence;
      await installed.ready();
      expect((await platform.listChannelProviders()).filter((row) => row.name === 'installed_probe'))
        .toMatchObject([{ name: 'installed_probe', label: 'Installed package channel' }]);
    }
    agentId = (await api.createAgent({
      name: `__e2e_pi_channel_background_${id}`,
      environment_name: profile.environment_name, model: profile.model, prewarm_enabled: false,
    })).agent_id;
    const deployment = await platform.createDeployment(agentId, {
      scene: scenario.installed ? 'channel:installed_probe' : 'channel:generic_json', prompt_prefix: '',
    });
    deploymentId = deployment.deployment_id;
    expect(deployment.secret).toBeTruthy();
    async function send(text: string, messageId: string): Promise<string> {
      const body = JSON.stringify(scenario.installed
        ? { utterance: text, event_id: messageId, room_id: id, callback_url: recipient.url }
        : { text, message_id: messageId, conversation_id: id, reply: { callback_url: recipient.url } });
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'content-type': 'application/json', ...(scenario.installed
          ? { 'x-installed-signature': createHmac('sha256', String(deployment.secret)).update(body).digest('hex') }
          : { 'x-channel-secret': String(deployment.secret) }) },
        data: body,
      });
      expect(response.ok(), await response.text()).toBe(true);
      const receipt = (await response.json()).data;
      expect(receipt.status).toBe('accepted');
      return String(receipt.session_id || '');
    }
    function deliveries() {
      expect(recipient.errors).toEqual([]);
      return recipient.deliveries.map((row) => {
        if (!scenario.installed) return row.text;
        expect(row.text).toMatch(/^\[installed-package\] /);
        return row.text.slice('[installed-package] '.length);
      });
    }
    if (scenario.installed) {
      const denied = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        data: { utterance: 'unauthenticated input must not execute', event_id: id, room_id: id, callback_url: recipient.url },
      });
      expect(denied.status()).toBe(401);
      expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toEqual([]);
    }
    const greeting = `Reply exactly READY_${id}. Do not use tools.`;
    sessionId = await send(greeting, `${id}-greeting`);
    expect(sessionId).not.toBe('');
    sessions.push(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([expect.stringContaining(`READY_${id}`)]);
    await api.waitForSessionReady(sessionId);
    if (profile.modes.unattended) await api.setPermissionMode(sessionId, profile.modes.unattended);
    const detail = await api.adminSessionDetail(sessionId);
    const root = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const launched = `LAUNCHED_${id}`;
    const gate = {
      marker: `PI_CHANNEL_${id}`, started: `${root}/.${id}.started`,
      release: `${root}/.${id}.release`, completed: `${root}/.${id}.completed`,
    };
    async function readGate(path: string): Promise<string | null> {
      const files = await platform.listFiles(sessionId, root, 10_000);
      return files.entries?.some((entry) => entry.name === path.split('/').at(-1) && entry.kind === 'file')
        ? api.downloadFileText(sessionId, path, 10_000) : null;
    }
    const prompt = [
      childPrompt(profile, 'background', gate),
      `Your initial acknowledgement must contain exactly ${launched}.`,
      'When the runtime delivers the child settlement notice, send a new reply containing its actual stdout receipt.',
      'Do not poll or call more tools to collect it. Wait for the native notification.',
    ].join('\n');
    expect(await send(prompt, `${id}-launch`)).toBe(sessionId);
    await expect.poll(deliveries, { timeout: 60_000 }).toEqual([
      expect.stringContaining(`READY_${id}`), expect.stringContaining(launched),
    ]);
    const initialDeliveries = deliveries();
    await expect.poll(() => launchEvidence(sessionId, profile, gate)).toHaveLength(1);
    const launches = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launches, profile, 'background');
    rootScope = launches[0]!.subpath;
    // Pi retains the workflow and its worker as distinct native runs.
    evidence.runningChildren = await api.waitForChildRuns(sessionId, (rows) => rows.length === 2
      && rows.every((row) => row.active)
      && rows.filter((row) => row.task_type === 'workflow').length === 1
      && rows.filter((row) => row.task_type === 'subagent').length === 1, 15_000);
    await expect.poll(() => readGate(gate.started)).toBe(gate.marker);
    expect((await api.getSession(sessionId)).current_turn_id).toBeFalsy();
    const initialHistory = await api.getMessages(sessionId, 50);
    evidence.initialHistory = initialHistory;
    evidence.launches = launches;
    const nativeFloor = Math.max(...nativeEntries(sessionId, String(rootScope)).map((row) => row.seq));
    const receipt = `COMPLETED_${randomUUID()}`;
    expect(JSON.stringify(initialHistory)).not.toContain(receipt);
    if (scenario.restart) {
      await restartServerContainer(absoluteBaseUrl(), 60_000, installed?.container);
      expect(deliveries()).toEqual(initialDeliveries);
    }
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, receipt);
    await expect.poll(() => readGate(gate.completed), { timeout: 30_000 }).toBe(receipt);

    const native = () => nativeEntries(sessionId, String(rootScope));
    const notices = () => native().filter((row) => row.seq > nativeFloor
      && row.entry.type === 'custom_message' && row.entry.customType === 'subagent-notify');
    await expect.poll(() => notices().length, { timeout: 30_000 }).toBeGreaterThan(0);
    const nativeReplies = () => finalReplies(native(), nativeFloor);
    await expect.poll(() => nativeReplies().some((reply) => reply.text.includes(receipt)), {
      timeout: 30_000,
    }).toBe(true);
    const expectedReplies = nativeReplies().map((reply) => reply.text.trim());
    expect(nativeReplies().find((reply) => reply.text.includes(receipt))!.seq)
      .toBeGreaterThan(notices()[0]!.seq);
    evidence.expectedReplies = expectedReplies;
    evidence.notices = notices().map((row) => ({ seq: row.seq, text: nativeText(row.entry.content) }));
    const completionReplies = async () => (await api.getMessages(sessionId, 50)).messages
      .filter((message) => message.role === 'assistant'
        && !initialHistory.messages.some((old) => old.message_id === message.message_id));
    await expect.poll(async () => (await completionReplies()).map((message) => messageText(message).trim()), {
      timeout: 30_000,
    }).toEqual(expectedReplies);
    await expect.poll(deliveries, { timeout: 30_000 }).toEqual([...initialDeliveries, ...expectedReplies]);
    const final = await api.getMessages(sessionId, 50);
    expect(final.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([greeting, prompt]);
    expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toHaveLength(2);
    await expect.poll(() => documentsByField('channel_outbox', '$.session_id', sessionId)
      .filter((row) => row.state === 'DELIVERED').length).toBe(initialDeliveries.length + expectedReplies.length);
    expect(final.messages.filter((message) => message.role === 'assistant')
      .map((message) => messageText(message).trim())).toEqual(deliveries());
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: receipt })).toBeVisible();
    if (scenario.restart) {
      const subscription = () => documentsByField('channel_output_subscriptions', '$.session_id', sessionId)[0];
      const completionCursor = Math.max(...(await completionReplies())
        .flatMap((message) => framesForTurn(String(message.turn_id)))
        .map((row) => Number(row.event_seq)));
      expect(Number.isFinite(completionCursor)).toBe(true);
      await expect.poll(() => Number(subscription()?.after_seq), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(completionCursor);
      const deliveredRows = documentsByField('channel_outbox', '$.session_id', sessionId);
      const server = installed?.container ?? requireServiceContainer(SERVER_CONTAINER_HANDLE);
      await page.goto('about:blank');
      stoppedServer = server;
      execFileSync('docker', ['stop', '--time', '10', server], { timeout: 30_000 });
      const saved = subscription();
      // Force durable delivery replay while the reader is stopped. The same
      // replies must keep their identities after the second cold attachment.
      expect(replaceDocs('channel_output_subscriptions', { '$.session_id': sessionId }, { ...saved, after_seq: 0 }))
        .toEqual([saved]);
      await restartServerContainer(absoluteBaseUrl(), 60_000, server);
      stoppedServer = '';
      await expect.poll(() => Number(subscription()?.after_seq), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(Number(saved.after_seq));
      expect(deliveries()).toEqual([...initialDeliveries, ...expectedReplies]);
      expect(documentsByField('channel_outbox', '$.session_id', sessionId)).toEqual(deliveredRows);
      const reloaded = await api.getMessages(sessionId, 50);
      expect(reloaded.messages.map((message) => message.message_id))
        .toEqual(final.messages.map((message) => message.message_id));
      await openSessionView(page, sessionId);
      await expect(page.getByTestId('assistant-text').filter({ hasText: receipt })).toBeVisible();
      evidence.completedReplyRestart = { completionCursor, afterSeq: subscription()?.after_seq };
    }
    if (scenario.installed) {
      const webFloor = Math.max(...native().map((row) => row.seq));
      const webReceipt = `WEB_${randomUUID()}`;
      await sendPrompt(page, sessionId, `Reply exactly ${webReceipt}. Do not use tools.`);
      const webReplies = () => finalReplies(native(), webFloor).map((reply) => reply.text.trim());
      await expect.poll(webReplies, { timeout: 30_000 }).toEqual([webReceipt]);
      await expect.poll(deliveries, { timeout: 30_000 })
        .toEqual([...initialDeliveries, ...expectedReplies, ...webReplies()]);
      await expect(page.getByTestId('assistant-text').filter({ hasText: webReceipt })).toBeVisible();
      expect(documentsByField('channel_inbound', '$.deployment_id', deploymentId)).toHaveLength(2);
      evidence.webReplies = webReplies();
    }
    evidence.native = native();
    completed = true;
  } finally {
    try {
      if (stoppedServer) await restartServerContainer(absoluteBaseUrl(), 60_000, stoppedServer);
      await info.attach('pi-channel-background-scene', {
        body: JSON.stringify({ ...evidence, sessionId, agentId, deploymentId, deliveries: recipient.deliveries,
          recipientErrors: recipient.errors,
          native: sessionId ? nativeRows(sessionId) : [],
          events: sessionId ? sessionEvents(sessionId) : [],
          outbox: sessionId ? documentsByField('channel_outbox', '$.session_id', sessionId) : [],
        }), contentType: 'application/json',
      });
    } finally {
      try {
        if (installed && deploymentId) {
          if (completed) {
            await platform.deleteDeployment(agentId, deploymentId);
            deploymentId = '';
          } else {
            await platform.updateDeployment(agentId, deploymentId, { enabled: false });
          }
        }
      } finally {
        try {
          if (installed) {
            await installed.restore();
            expect((await platform.listChannelProviders()).filter((row) => row.name === 'installed_probe')).toEqual([]);
            await info.attach('installed-channel-package', {
              body: JSON.stringify(installed.evidence), contentType: 'application/json',
            });
          }
        } finally {
          await recipient.close();
        }
      }
    }
  }
});
