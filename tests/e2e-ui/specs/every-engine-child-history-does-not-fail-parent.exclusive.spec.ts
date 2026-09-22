/** Native child history reads must not turn successful delegation into a failed parent turn. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { expectChildToolCard, expectToolBlocks } from '../fixtures/childToolAssertions';
import { documentsByField, framesForTurn, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { engineCases, engineProfileFor } from '../fixtures/engineProfile';
import {
  childPrompt, childToolEvidence, expectNativeMode, launchEvidence, nativeRows, type ChildGate,
} from '../fixtures/nativeChildLifecycle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let observations: unknown[] = [];
test.beforeEach(() => { sessionId = ''; observations = []; });
test.afterEach(async ({ request, page }, info) => {
  if (!sessionId || !['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const reads = await Promise.allSettled([
    api.getSession(sessionId), api.getMessages(sessionId, 50), aiStreamBodies(page),
    Promise.resolve().then(() => nativeRows(sessionId)),
    Promise.resolve().then(() => documentsByField('session_events', '$.session_id', sessionId)),
  ]);
  await info.attach('child-history-parent-failure-scene', {
    body: JSON.stringify({ sessionId, observations, reads }), contentType: 'application/json',
  });
});

async function expectNoFailure(page: Page, turnIds: string[], requireLiveFrames = true): Promise<void> {
  const bodies = await aiStreamBodies(page);
  const browserFrames = bodies.flatMap((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1).split('\n').flatMap((line) => {
    if (!line.startsWith('data:')) return [];
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') return [];
    return [JSON.parse(payload) as Record<string, unknown>];
  }));
  // Inspect every received frame, including a banner that disappeared before
  // the final DOM read. Preserve these bodies before reload clears the mirror.
  observations.push({ browserBodies: bodies });
  if (requireLiveFrames) {
    expect(browserFrames.length, 'the browser must actually consume the live channel').toBeGreaterThan(0);
  }
  expect(browserFrames.filter((frame) => ['error', 'data-turn-failure'].includes(String(frame.type))),
    'child history must not send even a transient parent failure to the browser').toEqual([]);
  expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed'),
    'READY cannot hide a durably failed turn').toEqual([]);
  for (const turnId of turnIds) {
    expect(framesForTurn(turnId).filter((event) => {
      const frame = event.payload as Record<string, unknown>;
      return ['error', 'data-turn-failure'].includes(String(frame.type));
    }), `turn ${turnId} must never journal a failure frame`).toEqual([]);
  }
  await expect(page.getByTestId('run-view').getByText(
    /本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了|This turn failed|list_turns is not supported yet/i,
  )).toHaveCount(0);
}

for (const declared of engineCases().filter((profile) => profile.contracts.background_subagent === true)) {
  test(`${declared.engine_kind} child history preserves the parent turn and tools under default permissions`, async ({ page, request }) => {
    const profile = engineProfileFor(declared.engine_kind);
    const api = new AstraApi(request);
    sessionId = (await api.startConversation(profile.agent_id)).session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    test.info().annotations.push({ type: 'engine_kind', description: profile.engine_kind });
    await api.waitForSessionReady(sessionId);
    // The reported Codex conversation used its normal workspace-write mode;
    // the previous eight lifecycle cases all selected danger-full-access.
    if (profile.engine_kind === 'codex') {
      expect(profile.modes.alternate).toBe('workspace-write');
      await api.setPermissionMode(sessionId, profile.modes.alternate!);
    }
    const detail = await api.adminSessionDetail(sessionId);
    if (profile.engine_kind === 'codex') expect(detail.permission_mode).toBe('workspace-write');
    const root = String(detail.runtime_identity?.workspace_dir ?? '').replace(/\/+$/, '');
    expect(root).toMatch(/^\//);
    const marker = `CHILD_HISTORY_${profile.engine_kind}_${randomUUID().replaceAll('-', '')}`;
    const receipt = `RECEIPT_${randomUUID().replaceAll('-', '')}`;
    const gate: ChildGate = {
      marker, started: `${root}/.${marker}.started`, release: `${root}/.${marker}.release`,
      completed: `${root}/.${marker}.completed`,
    };
    // This regression does not need a held workload. The fixture supplies the
    // receipt before launch; only the actual child command can read and return it.
    await api.uploadFileText(sessionId, root, gate.release.split('/').at(-1)!, receipt, 10_000);
    await mirrorSseBodies(page);
    await page.setViewportSize({ width: 1440, height: 1000 });
    const catalogRead = page.waitForResponse((response) => response.request().method() === 'GET'
      && new URL(response.url()).pathname.endsWith(`/sessions/${sessionId}/child-runs`));
    await openSessionView(page, sessionId);
    await page.getByRole('tab', { name: /^Agents/ }).click();
    const emptyCatalog = await catalogRead;
    expect(emptyCatalog.status()).toBe(200);
    expect(await emptyCatalog.finished()).toBeNull();
    expect((await emptyCatalog.json()).data.child_runs).toEqual([]);
    const panel = page.getByTestId('subagent-agents-panel');
    await expect(panel.getByTestId('subagent-agent-row')).toHaveCount(0);
    const prompt = childPrompt(profile, 'foreground', gate);
    observations.push({ permissionMode: detail.permission_mode, gate, prompt });
    await sendPrompt(page, sessionId, prompt);
    const first = await api.waitForSession(sessionId, (session) => {
      if (session.last_turn_status === 'FAILED') {
        throw new Error(`Child history failed the parent: ${JSON.stringify(session)}`);
      }
      return Boolean(session.last_turn_id) && !session.current_turn_id
        && session.state === 'READY' && session.last_turn_status === 'COMPLETED';
    }, 90_000);
    const firstTurn = String(first.last_turn_id);
    await waitForTurnTerminalProof(sessionId, firstTurn, 'COMPLETED', 10_000);
    await expectNoFailure(page, [firstTurn]);
    await expect(panel.getByTestId('subagent-agent-row'),
      'the already-open catalog must discover the real child without a test-side read').not.toHaveCount(0);
    const launched = launchEvidence(sessionId, profile, gate);
    expectNativeMode(launched, profile, 'foreground');
    if (profile.engine_kind === 'codex') {
      expect(Object.keys(launched[0]!.input), 'use the same message-only native spawn as the reported scene').toEqual(['message']);
      const metadata = nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
        .filter((entry) => entry.type === 'session_meta').map((entry) => entry.payload as Record<string, unknown>);
      expect(metadata.some((entry) => entry.parent_thread_id && entry.history_mode === 'paginated'),
        'the regression must exercise the real paginated child history mode').toBe(true);
    }
    await expect.poll(() => childToolEvidence(sessionId, profile, gate, launched[0]!.subpath)
      .filter((tool) => Boolean(tool.result)), {
      timeout: 15_000, message: 'independent supplier history must retain the child command and its real result',
    }).toHaveLength(1);
    const native = childToolEvidence(sessionId, profile, gate, launched[0]!.subpath)[0]!;
    expect(await api.downloadFileText(sessionId, gate.started, 10_000)).toBe(marker);
    expect(await api.downloadFileText(sessionId, gate.completed, 10_000)).toBe(receipt);
    const children = (await api.listChildRuns(sessionId)).child_runs;
    const matches = await Promise.all(children.map(async (child) => ({
      child, transcript: await api.getChildRunMessages(sessionId, child.child_run_id),
    })));
    const owners = matches.filter(({ transcript }) => transcript.messages.some((message) =>
      message.content.some((block) => block.type === 'tool_use' && block.id === native.id)));
    expect(owners, 'the actual tool must belong to one child transcript, never just the root').toHaveLength(1);
    const { child, transcript } = owners[0]!;
    expect(child.active).toBe(false);
    expectToolBlocks(transcript, native, receipt);
    const row = panel.locator(`[data-child-run-id="${child.child_run_id}"]`);
    await row.click();
    await expectChildToolCard(page, native, gate, receipt);
    const firstHistory = await api.getMessages(sessionId, 50);
    const reply = firstHistory.messages.filter((message) => message.turn_id === firstTurn && message.role === 'assistant')
      .map(messageText).join('\n');
    expect(reply.trim(), 'the parent must finish its own answer after delegation').not.toBe('');
    observations.push({ first, launched, native, child, transcript, firstHistory });
    await page.getByTestId('subagent-close-button').click();
    const followup = `For completed work item ${marker}, briefly explain what the child accomplished from the result already available. No additional tools are needed.`;
    await sendPrompt(page, sessionId, followup);
    const second = await api.waitForSession(sessionId, (session) => {
      if (session.last_turn_status === 'FAILED') throw new Error(`Parent continuation failed: ${JSON.stringify(session)}`);
      return Boolean(session.last_turn_id) && session.last_turn_id !== firstTurn
        && !session.current_turn_id && session.state === 'READY' && session.last_turn_status === 'COMPLETED';
    }, 40_000);
    const secondTurn = String(second.last_turn_id);
    await waitForTurnTerminalProof(sessionId, secondTurn, 'COMPLETED', 10_000);
    const continued = await api.getMessages(sessionId, 50);
    expect(continued.messages.some((message) => message.turn_id === secondTurn
      && message.role === 'assistant' && messageText(message).trim()), 'the later user turn must receive a real answer').toBe(true);
    await expectNoFailure(page, [firstTurn, secondTurn]);
    // A browser reload alone retains the server's live adapter/cache. Evict
    // this completed session's runtime so native Store reconciliation meets
    // the already persisted live facts through a newly attached adapter.
    expect(await api.adminEvictRuntime(sessionId)).toEqual({ evicted: sessionId });
    const reattachedChildren = (await api.listChildRuns(sessionId)).child_runs;
    expect(reattachedChildren.map((entry) => entry.child_run_id)).toEqual(children.map((entry) => entry.child_run_id));
    expect((await api.getChildRunMessages(sessionId, child.child_run_id)).messages).toEqual(transcript.messages);
    expect((await api.getSession(sessionId)).last_turn_status).toBe('COMPLETED');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await page.getByRole('tab', { name: /^Agents/ }).click();
    await row.click();
    await expectChildToolCard(page, native, gate, receipt);
    const coldTranscript = await api.getChildRunMessages(sessionId, child.child_run_id);
    expectToolBlocks(coldTranscript, native, receipt);
    expect(coldTranscript.messages).toEqual(transcript.messages);
    expect((await api.getSession(sessionId)).last_turn_status).toBe('COMPLETED');
    await expectNoFailure(page, [firstTurn, secondTurn], false);
  });
}
