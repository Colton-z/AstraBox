/**
 * SessionStart hook output stays visible once across live/history handoff.
 *
 * The greeting is the runtime's own output with no message before it, so the
 * session records it as a response the runtime started itself
 * (`origin: engine`, no input ids): the first response on the session's
 * stream, and one assistant message in history with the same id.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, sseFrames } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

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

  // History can already contain the greeting at navigation, so the page alone
  // cannot tell whether the stream carries it. The session's stream read from
  // its beginning must: nothing precedes the greeting, so it is the first
  // response there, whenever the page happened to load.
  const replay = await api.resumeStream(sessionId, 0, 30_000);
  const start = sseFrames(replay.raw).find((frame) => frame.type === 'start');
  const greetingId = String(start?.messageId ?? '').trim();
  expect(greetingId, 'the greeting must reach the stream as a message with its own id').not.toEqual('');
  expect(start?.messageMetadata, 'no input asked for the greeting: the runtime started it itself')
    .toMatchObject({ origin: 'engine', input_ids: [] });
  expect(replay.errorText).toBeNull();
  expect(replay.text.trim(), 'the stream carries the greeting the hook printed').toBe(welcome);

  // A fresh page must render the greeting from history before any input exists.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(visibleWelcome, 'pre-turn history keeps the same welcome').toHaveCount(1);
  await expect(visibleWelcome).toBeVisible();
  expect((await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() === welcome
  )).map((message) => ({ id: message.id, origin: message.metadata?.origin }))).toEqual([
    { id: greetingId, origin: 'engine' },
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
  expect(durableWelcome[0]!.id, 'history keeps the id the stream gave the greeting').toBe(greetingId);
  expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(visibleWelcome, 'cold history must keep the greeting exactly once').toHaveCount(1);
  await expect(visibleWelcome).toBeVisible();
  await expect(page.getByText('SessionStart:startup', { exact: false })).toHaveCount(0);
  const reloaded = (await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && messageText(message).trim() === welcome
  ));
  expect(reloaded.map((message) => message.id)).toEqual([greetingId]);
});
