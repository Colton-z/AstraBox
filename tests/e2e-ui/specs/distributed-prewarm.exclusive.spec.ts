import { randomUUID } from 'node:crypto';

import { expect, test, type BrowserContext } from '@playwright/test';

import { AstraApi, messageText, visibleMessages, type SessionRecord } from '../fixtures/astraApi';
import { DistributedBackend } from '../fixtures/distributedBackend';
import { backendBrowserFlow, data, responseFor } from '../fixtures/crossHostBackend';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
let replica: DistributedBackend | undefined;
let privateContext: BrowserContext | undefined;
let evidence: Record<string, unknown> = {};
let ui: ReturnType<typeof backendBrowserFlow>;

test.beforeEach(() => { agentId = ''; replica = undefined; privateContext = undefined; evidence = {};
  ui = backendBrowserFlow(sessions, evidence, (id) => { agentId = id; });
});
test.afterEach(async ({}, info) => {
  if (replica) evidence.replica = replica.evidence;
  await info.attach('distributed-prewarm', { body: JSON.stringify(evidence), contentType: 'application/json' });
  await privateContext?.close();
});
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  replica?.remove();
});

test('two live backends share prepared capacity without crossing conversation files or history',
  async ({ browser, context }) => {
    replica = new DistributedBackend(`astrabox-probe-distributed-${randomUUID().slice(0, 8)}`);
    evidence.replica = replica.evidence;
    await replica.ready();
    const origins = [replica.primaryOrigin, replica.secondaryOrigin];
    const unsigned = await browser.newContext();
    try {
      const page = await unsigned.newPage();
      for (const origin of origins) {
        const response = await page.goto(`${origin}${apiPath('/agents')}`);
        expect(response?.status(), 'an unsigned browser must not gain access on either backend').toBe(401);
      }
    } finally { await unsigned.close(); }
    privateContext = await replica.authenticatedContext(browser, context);
    const a = await privateContext.newPage();
    const b = await privateContext.newPage();
    const agent = await ui.createAgent(a, origins[0]);
    const first = await ui.prepared(a, origins[0]);
    expect(await ui.prepared(b, origins[1])).toEqual(first);
    evidence.firstPrepared = first;
    const session1 = await ui.start(b, origins[1], agent);
    evidence.firstSession = ui.ownership(session1);
    expect(session1.sandbox_id, 'B claims the waiting box already observed through A').toBe(first.sandbox_id);
    expect(ui.ownership(await ui.detail(a, origins[0], session1.session_id))).toEqual(ui.ownership(session1));
    replica.assertUnchanged();

    const second = await ui.prepared(a, origins[0]);
    expect(second.client_pool_name).toBe(first.client_pool_name);
    expect(second.sandbox_id).not.toBe(first.sandbox_id);
    expect(await ui.prepared(b, origins[1])).toEqual(second);
    evidence.secondPrepared = second;
    const session2 = await ui.start(a, origins[0], agent);
    evidence.secondSession = ui.ownership(session2);
    expect(session2.sandbox_id, 'A claims the replacement already observed through B').toBe(second.sandbox_id);
    expect(session2.workspace_id).not.toBe(session1.workspace_id);
    expect(ui.ownership(await ui.detail(b, origins[1], session2.session_id))).toEqual(ui.ownership(session2));
    replica.assertUnchanged();

    const marker1 = `FIRST-${randomUUID()}`;
    const marker2 = `SECOND-${randomUUID()}`;
    const filename = 'distributed-owner.txt';
    await Promise.all([
      ui.terminal(b, origins[1], session1.session_id, `printf '%s\\n' '${marker1}' > ${filename} && cat ${filename}`, marker1),
      ui.terminal(a, origins[0], session2.session_id, `printf '%s\\n' '${marker2}' > ${filename} && cat ${filename}`, marker2),
    ]);
    await Promise.all([
      ui.terminal(a, origins[0], session1.session_id, `cat ${filename}`, marker1),
      ui.terminal(b, origins[1], session2.session_id, `cat ${filename}`, marker2),
    ]);
    evidence.files = { filename, first: marker1, second: marker2 };

    const subscription = responseFor(a, `/sessions/${session1.session_id}/ai-stream`);
    const subscribed = a.waitForRequest((request) => request.method() === 'GET'
      && new URL(request.url()).pathname === apiPath(`/sessions/${session1.session_id}/ai-stream`));
    await ui.history(a, origins[0], session1.session_id);
    expect(new URL((await subscribed).url()).origin).toBe(origins[0]);
    await ui.history(b, origins[1], session1.session_id);
    await expect(a.getByTestId('assistant-message')).toHaveCount(0);
    const prompt = `Research project ${marker1}: explain in one short sentence why diversification reduces concentration risk. Do not use tools.`;
    await sendPrompt(b, session1.session_id, prompt);
    const stream = await subscription;
    expect(stream.status()).toBe(200);
    expect(stream.headers()['content-type']).toContain('text/event-stream');
    await expect(a.getByTestId('user-message')).toHaveText(prompt);
    await expect(a.getByTestId('assistant-message').last().getByTestId('assistant-text')).not.toBeEmpty({ timeout: 60_000 });
    await expect(a.getByTestId('assistant-message').last()).not.toHaveAttribute('data-streaming', 'true', { timeout: 60_000 });
    const rendered = await a.getByTestId('assistant-message').last().getByTestId('assistant-text').innerText();
    expect(rendered.trim()).not.toBe('');
    const final = await ui.detail(b, origins[1], session1.session_id);
    expect(final.state).toBe('READY');
    expect(ui.ownership(final)).toEqual(ui.ownership(session1));
    const transcriptA = visibleMessages(await ui.history(a, origins[0], session1.session_id));
    const settledRead = responseFor(b, `/sessions/${session1.session_id}`);
    const transcriptB = visibleMessages(await ui.history(b, origins[1], session1.session_id));
    const settled = await data<SessionRecord>(await settledRead);
    expect(settled.last_turn_status, String(settled.last_error || '')).toBe('COMPLETED');
    expect(settled.current_turn_id || null).toBeNull();
    evidence.settledTurn = { sessionId: settled.session_id, turnId: settled.last_turn_id,
      status: settled.last_turn_status, currentTurnId: settled.current_turn_id };
    expect(transcriptB).toEqual(transcriptA);
    expect(transcriptA.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
    await expect(b.getByTestId('assistant-message').last().getByTestId('assistant-text')).toHaveText(rendered);
    expect(JSON.stringify(transcriptA)).not.toContain(marker2);
    expect(visibleMessages(await ui.history(a, origins[0], session2.session_id))).toEqual([]);
    expect(visibleMessages(await ui.history(b, origins[1], session2.session_id))).toEqual([]);
    expect(ui.ownership(await ui.detail(a, origins[0], session2.session_id))).toEqual(ui.ownership(session2));
    evidence.history = transcriptA;
    replica.assertUnchanged();
  });
