/** SessionStart hook output stays visible once across live/history handoff. */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { sessionEvents } from '../fixtures/dbOracle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

test('SessionStart systemMessage appears once before a turn and after history reload', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const welcome = `SESSION_START_WELCOME_${runId}`;
  const hookReceipt = `/workspace/.astrabox-e2e-hook-${runId}`;
  const hookCommand = [
    "python3 -c 'from pathlib import Path; import json; ",
    `Path(${JSON.stringify(hookReceipt)}).write_text(${JSON.stringify(welcome)}); `,
    `print(json.dumps({"systemMessage": ${JSON.stringify(welcome)}}))'`,
  ].join('');
  const inlineSettings = {
    hooks: {
      SessionStart: [{
        matcher: 'startup',
        hooks: [{ type: 'command', command: hookCommand, timeout: 10 }],
      }],
    },
  };
  const agent = await api.createColdTestAgent(`e2e-session-start-${runId}`);
  agentId = agent.agent_id;
  await api.updateAgent(agentId, {
    name: agent.name,
    model: agent.model,
    environment_name: agent.environment_name,
    version: agent.version,
    engine_options: {
      sdk_options: {
        include_hook_events: true,
        settings: inlineSettings,
      },
    },
  });
  const session = await api.startConversation(agentId);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await api.waitForSessionReady(sessionId);

  // This file is written by the actual hook, not by the model or the test.
  // A missing hook must fail as setup evidence, not as a UI projection timeout.
  const receipt = await api.runTerminalCommand(sessionId, `cat ${hookReceipt}`, '/workspace');
  expect(receipt, 'the real startup hook must have run').toContain(welcome);
  const beforeFirstTurn = await api.getMessages(sessionId);
  expect(beforeFirstTurn.messages.filter((message) => (
    message.role === 'user'
  )), 'the welcome must not require an invented user input').toHaveLength(0);
  expect(beforeFirstTurn.messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() !== welcome
  )), 'startup must not fabricate an assistant answer before the first user turn').toHaveLength(0);

  const visibleWelcome = page.getByText(welcome, { exact: true });
  await expect(visibleWelcome, 'welcome before the first user turn').toHaveCount(1, {
    timeout: 30_000,
  });
  await expect(visibleWelcome).toBeVisible();
  await expect(page.getByText('SessionStart:startup', { exact: false })).toHaveCount(0);
  const hookMessages = sessionEvents(sessionId)
    .filter((event) => event.event_type === 'engine.message')
    .map((event) => (event.payload as { message?: Record<string, unknown> }).message)
    .filter((message) => (
      message?.__sdk_type === 'HookEventMessage'
      && message.subtype === 'hook_response'
      && message.hook_event_name === 'SessionStart'
      && String((message.data as { output?: unknown })?.output ?? '').includes(welcome)
    ));
  expect(hookMessages, 'the real supplier response must be durable before user input').toHaveLength(1);
  expect(JSON.parse(String((hookMessages[0]!.data as { output: string }).output))).toEqual({
    systemMessage: welcome,
  });
  const nativeEventId = String(hookMessages[0]!.uuid ?? '').trim();
  expect(nativeEventId).not.toEqual('');
  // History can already contain the greeting at navigation. Require its actual
  // browser stream payload too, so history cannot mask a broken live channel.
  await expect.poll(async () => (await aiStreamBodies(page))
    .flatMap((body) => body.text.split('\n').slice(0, -1))
    .filter((line) => line.startsWith('data: ') && line.trim() !== 'data: [DONE]')
    .map((line) => JSON.parse(line.slice(6)) as Record<string, unknown>)
    .filter((frame) => frame.type === 'data-session-message'), {
    timeout: 30_000,
    message: 'the real Session follower must publish the welcome before any user turn',
  }).toContainEqual(expect.objectContaining({
    id: `sdk-hook-system-message:${nativeEventId}`,
    transient: true,
    data: expect.objectContaining({ session_id: sessionId, turn_id: '', content: welcome }),
  }));

  // A fresh page must render a Session-owned record even before any turn exists.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(visibleWelcome, 'pre-turn history keeps the same welcome').toHaveCount(1);
  await expect(visibleWelcome).toBeVisible();
  expect((await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() === welcome
  )).map((message) => ({ id: message.message_id, turn: message.turn_id }))).toEqual([
    { id: `sdk-hook-system-message:${nativeEventId}`, turn: '' },
  ]);

  const before = await api.assistantCount(sessionId);
  const prompt = `Explain in one short sentence what a conversation greeting is. Do not use tools. Reference ${runId} is only a label.`;
  await sendPrompt(page, sessionId, prompt);
  await api.waitForAssistantMessageMatching(
    sessionId,
    before,
    (message) => messageText(message).trim().length > 0 && messageText(message).trim() !== welcome,
    60_000,
  );
  await api.waitForSessionReady(sessionId);
  const history = await api.getMessages(sessionId);
  const durableWelcome = history.messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() === welcome
  ));
  expect(durableWelcome, 'the hook greeting must have exactly one durable identity').toHaveLength(1);
  expect(String(durableWelcome[0]!.message_id || '').trim()).not.toEqual('');
  expect(durableWelcome[0]!.message_id).toBe(`sdk-hook-system-message:${nativeEventId}`);
  expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(visibleWelcome, 'cold history must keep the greeting exactly once').toHaveCount(1);
  await expect(visibleWelcome).toBeVisible();
  await expect(page.getByText('SessionStart:startup', { exact: false })).toHaveCount(0);
  const reloaded = (await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() === welcome
  ));
  expect(reloaded.map((message) => message.message_id)).toEqual([durableWelcome[0]!.message_id]);
});
