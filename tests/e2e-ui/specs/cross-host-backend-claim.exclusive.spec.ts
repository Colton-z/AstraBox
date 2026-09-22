import { randomUUID } from 'node:crypto';
import { expect, test, type BrowserContext } from '@playwright/test';

import { AstraApi, messageText, visibleMessages, type SessionRecord } from '../fixtures/astraApi';
import { backendBrowserFlow, CrossHostBackend, data, responseFor } from '../fixtures/crossHostBackend';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';
import { WorkspaceResumeNodes } from '../fixtures/workspaceResumeNodes';

let nodes: WorkspaceResumeNodes | undefined;
// Restore scheduling even when the failed conversation and backend are retained.
test.afterEach(() => { nodes?.restore(); });
const sessions = trackSessions();
let agentId = '';
let privateContext: BrowserContext | undefined;
let evidence: Record<string, unknown> = {};

test.beforeEach(() => { nodes = undefined; agentId = ''; privateContext = undefined; evidence = {}; });
test.afterEach(async ({}, info) => {
  await info.attach('cross-host-backend-claim', { body: JSON.stringify(evidence), contentType: 'application/json' });
  await privateContext?.close();
});
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('backend on B claims the prepared sandbox on A and serves its files and live turn',
  async ({ browser, context }) => {
    nodes = new WorkspaceResumeNodes();
    evidence.scheduling = nodes.evidence;
    const backend = new CrossHostBackend(nodes);
    evidence.backends = backend.evidence;
    const [originA, originB] = [backend.primaryOrigin, backend.secondaryOrigin];
    const unsigned = await browser.newContext();
    try {
      const page = await unsigned.newPage();
      for (const origin of [originA, originB]) {
        expect((await page.goto(`${origin}${apiPath('/agents')}`))?.status()).toBe(401);
      }
    } finally { await unsigned.close(); }
    privateContext = await backend.authenticatedContext(browser, context);
    const a = await privateContext.newPage();
    const b = await privateContext.newPage();
    const ui = backendBrowserFlow(sessions, evidence, (id) => { agentId = id; });
    nodes.prepareSource();
    const agent = await ui.createAgent(a, originA);
    const waiting = await ui.prepared(a, originA);
    evidence.prepared = waiting;
    const placement = backend.sandboxOnSource(waiting.sandbox_id);
    evidence.beforeClaim = placement;
    expect(await ui.prepared(b, originB)).toEqual(waiting);

    const creation = responseFor(b, `/agents/${agentId}/conversations`, 'POST');
    const session = await ui.start(b, originB, agent);
    const created = await creation;
    expect(new URL(created.url()).origin).toBe(originB);
    expect((await data<{ session_id: string }>(created)).session_id).toBe(session.session_id);
    evidence.claim = { requestOrigin: new URL(created.url()).origin, ...ui.ownership(session) };
    expect(session.sandbox_id, 'B must claim the exact waiting box, not create a replacement').toBe(waiting.sandbox_id);
    expect(backend.sandboxOnSource(session.sandbox_id)).toEqual(placement);
    expect(ui.ownership(await ui.detail(a, originA, session.session_id))).toEqual(ui.ownership(session));
    backend.assertUnchanged();

    const marker = `CROSS-HOST-${randomUUID()}`;
    const filename = 'cross-host-backend.txt';
    await ui.terminal(b, originB, session.session_id,
      `printf '%s\\n' '${marker}' > ${filename} && cat ${filename}`, marker);
    await ui.terminal(a, originA, session.session_id, `cat ${filename}`, marker);
    evidence.file = { filename, content: marker };
    await ui.history(b, originB, session.session_id);
    const prompt = `Research project ${marker}: explain in one short sentence why diversification reduces concentration risk. Do not use tools.`;
    const accepted = await sendPrompt(b, session.session_id, prompt);
    expect(new URL(accepted.url()).origin).toBe(originB);
    await expect(b.getByTestId('user-message')).toHaveText(prompt);
    const reply = b.getByTestId('assistant-message').last();
    await expect(reply.getByTestId('assistant-text')).not.toBeEmpty({ timeout: 60_000 });
    await expect(reply).not.toHaveAttribute('data-streaming', 'true', { timeout: 60_000 });
    const rendered = await reply.getByTestId('assistant-text').innerText();
    const settledRead = responseFor(b, `/sessions/${session.session_id}`);
    const transcriptB = visibleMessages(await ui.history(b, originB, session.session_id));
    const settled = await data<SessionRecord>(await settledRead);
    expect(settled.last_turn_status, String(settled.last_error || '')).toBe('COMPLETED');
    expect(settled.current_turn_id || null).toBeNull();
    expect(settled.sandbox_id).toBe(waiting.sandbox_id);
    expect(visibleMessages(await ui.history(a, originA, session.session_id))).toEqual(transcriptB);
    expect(transcriptB.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
    await expect(a.getByTestId('assistant-message').last().getByTestId('assistant-text')).toHaveText(rendered);
    evidence.turn = { status: settled.last_turn_status, turnId: settled.last_turn_id, transcript: transcriptB };
    expect(backend.sandboxOnSource(session.sandbox_id)).toEqual(placement);
    backend.assertUnchanged();
  });
