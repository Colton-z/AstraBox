/** Native question answers resume their owning turn without another input. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText, type PendingInteraction } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn, sessionEvents } from '../fixtures/dbOracle';
import { events, messageProseBlocks, textBlockValues, textBlocks } from '../fixtures/dshOutput';
import type { NativeEvent } from '../fixtures/dshOutput';
import { savedOutput } from '../fixtures/dshNativeApproval';
import { enterDshNativePlanMode } from '../fixtures/dshNativePlan';
import { engineProfileFor } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

interface Question {
  id: string;
  header: string;
  question: string;
  options: { label: string; description: string }[];
  multi_select: boolean;
}

const sessions = trackSessions();
let agentId = '';
test.beforeEach(() => { agentId = ''; });
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function nativeSource(sessionId: string): NativeEvent[] {
  return nativeRows(sessionId).flatMap((row) => events(JSON.parse(String(row.entry_json))))
    .sort((left, right) => left.seq - right.seq);
}

async function showQuestion(page: Page, question: Question): Promise<void> {
  const panel = page.getByTestId('pending-interaction-panel');
  await panel.locator('[data-slot="pending-interaction-question-navigation"]')
    .getByRole('button').filter({ hasText: question.header }).click();
  const group = panel.getByRole('group', { name: question.header, exact: true });
  await expect(group).toBeVisible();
  await expect(group).toContainText(question.question);
  for (const option of question.options) {
    await expect(group.getByRole(question.multi_select ? 'checkbox' : 'radio', {
      name: new RegExp(option.label),
    })).toBeVisible();
    await expect(group).toContainText(option.description);
  }
}

function expectConsumption(
  sessionId: string, turnId: string, clientMessageId: string, prompt: string,
): { inputId: string; messageId: string } {
  const journal = sessionEvents(sessionId);
  const accepted = journal.filter((row) => row.event_type === 'command.accepted'
    && (row.payload as Record<string, unknown>).client_message_id === clientMessageId);
  expect(accepted).toEqual([expect.objectContaining({ turn_id: turnId,
    payload: expect.objectContaining({ content: prompt, client_message_id: clientMessageId }),
  })]);
  const inputId = String((accepted[0]!.payload as Record<string, unknown>).input_id || '');
  expect(inputId).not.toBe('');
  const consumed = journal.filter((row) => row.event_type === 'input.consumed'
    && (row.payload as Record<string, unknown>).input_id === inputId);
  expect(consumed).toHaveLength(1);
  expect(consumed[0]).toMatchObject({ turn_id: turnId,
    payload: { client_message_id: clientMessageId, content: prompt },
  });
  const messageId = String((consumed[0]!.payload as Record<string, unknown>).response_message_id || '');
  expect(messageId).not.toBe('');
  expect(framesForTurn(turnId).map((row) => row.payload as Record<string, unknown>)
    .filter((frame) => frame.type === 'data-input-consumed')).toEqual([
    expect.objectContaining({ data: expect.objectContaining({
      inputId, responseMessageId: messageId, content: prompt,
    }) }),
  ]);
  return { inputId, messageId };
}

const cases = [
  { kind: 'choices', title: 'DSH preserves native single and multiple selections across a pending-question reload' },
  { kind: 'custom', title: 'DSH delivers a custom answer as native text rather than an undeclared selection' },
  { kind: 'decline', title: 'DSH records native question cancellation and accepts the next message' },
] as const;

for (const scenario of cases) test(scenario.title, async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const marker = randomUUID().replaceAll('-', '');
  const receipt = `QUESTION_DONE_${marker}`;
  const questions: Question[] = [
    { id: `store-${marker}`, header: `Store ${marker}`, question: `Choose storage for ${marker}.`,
      options: [
        { label: 'PostgreSQL', description: `Transactional storage ${marker}.` },
        { label: 'SQLite', description: `Embedded storage ${marker}.` },
      ], multi_select: false },
    { id: `search-${marker}`, header: `Search ${marker}`, question: `Choose search features for ${marker}.`,
      options: [
        { label: 'PostGIS', description: `Geospatial search ${marker}.` },
        { label: 'pgvector', description: `Vector search ${marker}.` },
      ], multi_select: true },
  ];
  const prompt = `Call ask_user_question exactly once with exactly these arguments: ${JSON.stringify({ questions })}. `
    + `After the user answers or declines, reply exactly ${receipt}. Do not use other tools or delegate.`;
  const evidence: Record<string, unknown> = { scenario: scenario.kind, questions, prompt };
  let sessionId = '';
  let pending: PendingInteraction | null = null;
  try {
    agentId = (await api.createAgent({ name: `__e2e_dsh_question_${marker}`,
      environment_name: profile.environment_name, model: profile.model, prepared_capacity: 0 })).agent_id;
    sessionId = (await api.startConversation(agentId)).session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    expect((await api.getSession(sessionId)).engine_capabilities).toMatchObject({ supports_interaction: true });
    await openSessionView(page, sessionId);
    const delivery = await sendPrompt(page, sessionId, prompt);
    const clientMessageId = String(delivery.request().postDataJSON().client_message_id || '');
    expect(clientMessageId).not.toBe('');
    pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 60_000);
    expect(pending, 'the real tool must park on its question batch').not.toBeNull();
    const interaction = pending!;
    const turnId = String(interaction.turn_id || '');
    expect(turnId).not.toBe('');
    expect(interaction).toMatchObject({ presentation: 'form', tool_name: 'ask_user_question',
      prompt: questions[0]!.header, questions: questions.map((question) => ({ ...question,
        allow_free_text: true, allow_empty_text: false,
      })),
    });
    const raw = interaction.raw_input as Record<string, unknown>;
    expect(raw.questions).toEqual(questions.map(({ multi_select, ...question }) => ({
      ...question, multiSelect: multi_select,
    })));
    const headers = nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
      .filter((entry) => entry.type === 'session');
    expect(headers).toHaveLength(1);
    expect(raw.sessionId).toBe(headers[0]!.id);
    await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'tool/call'))
      .toHaveLength(1);
    const call = nativeSource(sessionId).find((event) => event.type === 'tool/call')!;
    expect(call.data).toMatchObject({ name: 'ask_user_question', callId: expect.any(String), turn: expect.any(Number) });
    expect(call.data.callId).not.toBe('');
    expect(JSON.parse(String(call.data.arguments))).toEqual({ questions });
    const parked = nativeSource(sessionId);
    expect(parked.filter((event) => event.type === 'turn/start')).toEqual([
      expect.objectContaining({ data: expect.objectContaining({ turn: call.data.turn }) }),
    ]);
    expect(parked.filter((event) => event.type === 'turn/end')).toEqual([]);
    const originalConsumption = expectConsumption(sessionId, turnId, clientMessageId, prompt);
    evidence.parked = { pending: interaction, native: parked, consumption: originalConsumption };

    for (const reload of [false, true]) {
      if (reload) await page.reload();
      await expect(page.getByTestId('pending-interaction-panel')).toBeVisible();
      const restored = await api.getPendingInteraction(sessionId);
      expect(restored).toMatchObject({ interaction_id: interaction.interaction_id, turn_id: turnId,
        questions: interaction.questions, raw_input: raw,
      });
      for (const question of questions) await showQuestion(page, question);
    }
    const panel = page.getByTestId('pending-interaction-panel');
    await showQuestion(page, questions[0]!);
    const first = panel.getByRole('group', { name: questions[0]!.header, exact: true });
    const custom = `MySQL, actually ${marker}`;
    if (scenario.kind === 'choices') {
      await first.getByRole('radio', { name: /SQLite/ }).click();
    } else {
      await first.getByRole('radio', { name: /Write your own|自己补充/ }).click();
      if (scenario.kind === 'custom') await first.getByRole('textbox').fill(custom);
    }
    const expectedAnswers = [
      { question_id: questions[0]!.id, ...(scenario.kind === 'custom'
        ? { free_text: custom } : { option_label: 'SQLite' }) },
      { question_id: questions[1]!.id, option_labels: ['pgvector', 'PostGIS'] },
    ];
    if (scenario.kind !== 'decline') {
      await showQuestion(page, questions[1]!);
      const second = panel.getByRole('group', { name: questions[1]!.header, exact: true });
      for (const label of ['pgvector', 'PostGIS']) {
        const choice = second.getByRole('checkbox', { name: new RegExp(label) });
        await choice.click();
        await expect(choice).toBeChecked();
      }
    }
    const response = page.waitForResponse((value) => value.request().method() === 'POST'
      && new URL(value.url()).pathname === apiPath(`/sessions/${sessionId}/interaction-respond`));
    await panel.getByRole('button', { name: scenario.kind === 'decline'
      ? /Decline to answer|拒绝回答/ : /Submit answer|提交回答/ }).click();
    const answered = await response;
    expect(answered.status()).toBe(200);
    const body = answered.request().postDataJSON() as Record<string, unknown>;
    expect(body).toEqual({ interaction_id: interaction.interaction_id,
      answer: { interaction_id: interaction.interaction_id,
        ...(scenario.kind === 'decline' ? { decline: true } : { answers: expectedAnswers }) },
    });
    expect((await answered.json()).data).toMatchObject({ interaction_id: interaction.interaction_id, answered: true });
    evidence.browserAnswer = body;
    await api.waitForAssistantMessageMatching(sessionId, 0, (message) => messageText(message).includes(receipt));
    await api.waitForSessionReady(sessionId);
    await expect(panel).toHaveCount(0);
    await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(1);
    const source = nativeSource(sessionId);
    const results = source.filter((event) => event.type === 'tool/result');
    expect(results).toHaveLength(1);
    const result = results[0]!;
    expect(result.data.turn).toBe(call.data.turn);
    const tool = result.data.message as Record<string, unknown>;
    expect(tool).toMatchObject({ role: 'tool', toolCallId: call.data.callId,
      isError: scenario.kind === 'decline',
    });
    if (scenario.kind === 'decline') {
      // The supplier saves structured HarnessError identity beside the message.
      expect(result.data.error).toEqual({ name: 'UserQuestionError', code: 'ASK_CANCELLED' });
      expect(textBlocks(tool.content)).toBe('Error: the user cancelled ask_user_question');
    } else {
      expect(result.data.error).toBeUndefined();
      expect(JSON.parse(textBlocks(tool.content))).toEqual({ answers: [
        { id: questions[0]!.id, selected: scenario.kind === 'custom' ? [] : ['SQLite'],
          ...(scenario.kind === 'custom' ? { custom } : {}) },
        { id: questions[1]!.id, selected: ['pgvector', 'PostGIS'] },
      ] });
    }
    expect(source.filter((event) => event.type === 'turn/start')).toHaveLength(1);
    expect(source.filter((event) => event.type === 'turn/end')).toEqual([
      expect.objectContaining({ data: expect.objectContaining({ turn: call.data.turn, reason: { kind: 'completed' } }) }),
    ]);
    const nativeInput = source.filter((event) => event.type === 'user/message'
      && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user');
    expect(nativeInput).toHaveLength(1);
    expect(textBlocks(nativeInput[0]!.data.content)).toBe(prompt);
    expect(expectConsumption(sessionId, turnId, clientMessageId, prompt)).toEqual(originalConsumption);
    const frames = framesForTurn(turnId).map((row) => row.payload as Record<string, unknown>);
    expect(frames.filter((frame) => frame.type === 'tool-input-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, toolName: 'ask_user_question', input: { questions } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId,
        output: { content: tool.content, isError: scenario.kind === 'decline' } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'error')).toEqual([]);
    expect(framesForTurn(turnId).filter((row) => (row.payload as Record<string, unknown>).type === 'data-result')).toEqual([
      expect.objectContaining({
        engine_kind: 'deepseek_harness',
        engine_turn_id: (nativeInput[0]!.data.source as Record<string, unknown>).rpcId,
        payload: expect.objectContaining({ data: expect.objectContaining({ finish_reason: 'stop' }) }),
      }),
    ]);
    expect(frames.filter((frame) => frame.type === 'finish').at(-1)).toMatchObject({ finishReason: 'stop' });
    const history = (await api.getMessages(sessionId)).messages;
    const retained = history.filter((message) => message.role === 'assistant' && message.turn_id === turnId);
    expect(retained).toHaveLength(1);
    expect(retained[0]!.message_id).toBe(originalConsumption.messageId);
    const nativeMessages = source.filter((event) => event.type === 'assistant/message');
    expect(messageProseBlocks(retained[0]!)).toEqual(nativeMessages.flatMap((event) =>
      textBlockValues((event.data.message as Record<string, unknown>).content)));
    expect(textBlocks((nativeMessages.at(-1)!.data.message as Record<string, unknown>).content)).toBe(receipt);
    expect((retained[0]!.blocks ?? []).filter((block) => block.type === 'tool_use')).toEqual([
      expect.objectContaining({ id: call.data.callId, name: 'ask_user_question',
        input: JSON.parse(String(call.data.arguments)) }),
    ]);
    expect(tool.content).toEqual([{ type: 'text', text: textBlocks(tool.content) }]);
    expect((retained[0]!.blocks ?? []).filter((block) => block.type === 'tool_result')).toEqual([
      expect.objectContaining({ tool_use_id: call.data.callId,
        content: expect.stringContaining(textBlocks(tool.content)) }),
    ]);
    const storedBlocks = JSON.stringify(retained[0]!.blocks);
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      const reply = page.locator(`[data-message-id="${originalConsumption.messageId}"]`);
      await expect(reply).toContainText(receipt);
      await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
      await revealAssistantProcess(page, { within: reply });
      const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
      await expect(card).toBeVisible();
      const trigger = card.locator('button[aria-expanded]').first();
      if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
      await expect(card).toContainText(scenario.kind === 'decline'
        ? 'the user cancelled ask_user_question' : scenario.kind === 'custom' ? custom : 'SQLite');
      if (scenario.kind !== 'decline') {
        await expect(card).toContainText('pgvector');
        await expect(card).toContainText('PostGIS');
      }
      const reread = (await api.getMessages(sessionId)).messages
        .find((message) => message.message_id === originalConsumption.messageId);
      expect(JSON.stringify(reread!.blocks)).toBe(storedBlocks);
    }
    evidence.completed = { native: source, frames, history };
    if (scenario.kind === 'decline') {
      const nextReceipt = `AFTER_DECLINE_${marker}`;
      const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
      const nextDelivery = await sendPrompt(page, sessionId, nextPrompt);
      const nextClientMessageId = String(nextDelivery.request().postDataJSON().client_message_id || '');
      const next = await api.waitForAssistantMessageMatching(sessionId, 0,
        (message) => messageText(message) === nextReceipt);
      await api.waitForSessionReady(sessionId);
      expect(next.turn_id).not.toBe(turnId);
      expect(next.message_id).not.toBe(originalConsumption.messageId);
      expectConsumption(sessionId, next.turn_id, nextClientMessageId, nextPrompt);
      await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(2);
      const terminal = source.find((event) => event.type === 'turn/end')!;
      const later = nativeSource(sessionId).filter((event) => event.seq > terminal.seq);
      const nextStart = later.filter((event) => event.type === 'turn/start');
      expect(nextStart).toHaveLength(1);
      expect(nextStart[0]!.data.turn).not.toBe(call.data.turn);
      expect(later.filter((event) => event.type === 'turn/end')).toEqual([
        expect.objectContaining({ data: expect.objectContaining({
          turn: nextStart[0]!.data.turn, reason: { kind: 'completed' },
        }) }),
      ]);
      expect(later.filter((event) => event.type === 'tool/call')).toEqual([]);
      expect(later.filter((event) => event.type === 'user/message').map((event) => textBlocks(event.data.content)))
        .toEqual([nextPrompt]);
      expect(later.filter((event) => event.type === 'assistant/message').flatMap((event) =>
        textBlockValues((event.data.message as Record<string, unknown>).content))).toEqual([nextReceipt]);
      const nextFrames = framesForTurn(next.turn_id).map((row) => row.payload as Record<string, unknown>);
      expect(nextFrames.filter((frame) => frame.type === 'error')).toEqual([]);
      expect(nextFrames.filter((frame) => frame.type === 'finish')).toEqual([
        expect.objectContaining({ finishReason: 'stop' }),
      ]);
      const laterHistory = (await api.getMessages(sessionId)).messages;
      expect(messageProseBlocks(laterHistory.find((message) => message.message_id === next.message_id)!))
        .toEqual([nextReceipt]);
      expect(JSON.stringify(laterHistory.find((message) => message.message_id === originalConsumption.messageId)!.blocks))
        .toBe(storedBlocks);
      const priorProcess = page.locator(`[data-message-id="${originalConsumption.messageId}"]`)
        .getByTestId('assistant-turn-process-trigger');
      await priorProcess.click();
      await expect(priorProcess).toHaveAttribute('aria-expanded', 'false');
      for (const reload of [false, true]) {
        if (reload) await page.reload();
        await expect(page.locator(`[data-message-id="${next.message_id}"]`)).toContainText(nextReceipt);
        await expect(page.locator(`[data-message-id="${originalConsumption.messageId}"]`)).toContainText(receipt);
        await expect(page.getByTestId('composer-prompt')).toBeEnabled();
      }
      evidence.next = { turnId: next.turn_id, native: later, frames: nextFrames, history: laterHistory };
    }
  } finally {
    if (sessionId) {
      evidence.sessionId = sessionId;
      evidence.agentId = agentId;
      evidence.pending = pending;
      for (const [key, read] of [
        ['nativeAtExit', () => nativeRows(sessionId)],
        ['journalAtExit', () => sessionEvents(sessionId)],
      ] as const) {
        try { evidence[key] = read(); }
        catch (error) { evidence[key] = { observationError: String(error) }; }
      }
    }
    await info.attach('dsh-native-question-scene', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});

test('DSH preserves complete native plan details and resumes the reviewed turn', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const marker = randomUUID().replaceAll('-', '');
  const receipt = `PLAN_REVIEW_DONE_${marker}`;
  const plan = `# Migration plan ${marker}\n\n`
    + `- Retain the original database and its saved records ${marker}.\n`
    + `- Replace the service image while retaining its encryption key ${marker}.\n`
    + `- Compare complete tool arguments, results, and usage after restart ${marker}.`;
  const question: Question = { id: 'plan-review', header: 'Plan review',
    question: `Approve this plan and leave plan mode?\n\n${plan}`,
    options: [
      { label: 'Approve', description: 'Leave plan mode; the plan is carried out from the next step.' },
      { label: 'Keep planning', description: 'Stay in plan mode; feedback goes back to the model.' },
    ], multi_select: false };
  const prompt = `Plan mode is active. Call exit_plan_mode exactly once with exactly these arguments: `
    + `${JSON.stringify({ plan })}. After the user approves, reply exactly ${receipt}. `
    + `Do not execute the plan, use any other tools, or delegate.`;
  const evidence: Record<string, unknown> = { plan, question, prompt };
  let sessionId = '';
  try {
    agentId = (await api.createAgent({ name: `__e2e_dsh_plan_detail_${marker}`,
      environment_name: profile.environment_name, model: profile.model, prepared_capacity: 0 })).agent_id;
    sessionId = (await api.startConversation(agentId)).session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const headers = nativeRows(sessionId).map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>)
      .filter((entry) => entry.type === 'session');
    expect(headers).toHaveLength(1);
    const nativeSessionId = String(headers[0]!.id || '');
    evidence.nativeCommand = await enterDshNativePlanMode(api, sessionId, nativeSessionId);
    await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'plan/mode')
      .map((event) => event.data.active)).toEqual([true]);
    expect(nativeSource(sessionId).filter((event) => event.type === 'turn/start')).toEqual([]);
    await openSessionView(page, sessionId);
    const delivery = await sendPrompt(page, sessionId, prompt);
    const clientMessageId = String(delivery.request().postDataJSON().client_message_id || '');
    expect(clientMessageId).not.toBe('');
    const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 60_000);
    expect(pending, 'the native review must wait for its human answer').not.toBeNull();
    const interaction = pending!;
    const turnId = String(interaction.turn_id || '');
    expect(turnId).not.toBe('');
    await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'tool/call')).toHaveLength(1);
    const call = nativeSource(sessionId).find((event) => event.type === 'tool/call')!;
    expect(call.data).toMatchObject({ name: 'exit_plan_mode', callId: expect.any(String), turn: expect.any(Number) });
    expect(call.data.callId).not.toBe('');
    expect(JSON.parse(String(call.data.arguments))).toEqual({ plan });
    expect(interaction).toMatchObject({ presentation: 'form', tool_name: call.data.name, tool_call_id: call.data.callId,
      prompt: question.header, questions: [{ ...question, allow_free_text: true, allow_empty_text: false }],
    });
    const raw = interaction.raw_input as Record<string, unknown>;
    expect(raw.sessionId).toBe(nativeSessionId);
    expect(raw.questions).toEqual([{ id: question.id, header: question.header,
      question: 'Approve this plan and leave plan mode?', detail: plan, options: question.options,
      intent: { kind: 'plan-review', approve: 'Approve', callId: call.data.callId },
    }]);
    const held = nativeSource(sessionId);
    expect(held.filter((event) => event.type === 'turn/start')).toEqual([
      expect.objectContaining({ data: expect.objectContaining({ turn: call.data.turn }) }),
    ]);
    expect(held.filter((event) => event.type === 'turn/end')).toEqual([]);
    expect(held.filter((event) => event.type === 'tool/result')).toEqual([]);
    const consumed = expectConsumption(sessionId, turnId, clientMessageId, prompt);
    evidence.held = { pending: interaction, native: held, consumption: consumed };
    const panel = page.getByTestId('pending-interaction-panel');
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      await expect(panel).toBeVisible();
      const restored = await api.getPendingInteraction(sessionId);
      expect(restored).toMatchObject({ interaction_id: interaction.interaction_id, turn_id: turnId,
        tool_name: call.data.name, tool_call_id: call.data.callId, questions: interaction.questions, raw_input: raw,
      });
      const group = panel.getByRole('group', { name: question.header, exact: true });
      await expect(group).toBeVisible();
      await expect(group).toContainText(question.question);
      for (const option of question.options) {
        await expect(group.getByRole('radio', { name: new RegExp(option.label) })).toBeVisible();
        await expect(group).toContainText(option.description);
      }
    }
    await panel.getByRole('radio', { name: /Approve/ }).click();
    const response = page.waitForResponse((value) => value.request().method() === 'POST'
      && new URL(value.url()).pathname === apiPath(`/sessions/${sessionId}/interaction-respond`));
    await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
    const answered = await response;
    expect(answered.status()).toBe(200);
    expect(answered.request().postDataJSON()).toEqual({ interaction_id: interaction.interaction_id,
      answer: { interaction_id: interaction.interaction_id,
        answers: [{ question_id: question.id, option_label: 'Approve' }] },
    });
    expect((await answered.json()).data).toMatchObject({ interaction_id: interaction.interaction_id, answered: true });
    await api.waitForAssistantMessageMatching(sessionId, 0, (message) => messageText(message) === receipt);
    await api.waitForSessionReady(sessionId);
    await expect(panel).toHaveCount(0);
    await expect.poll(() => nativeSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(1);
    const source = nativeSource(sessionId);
    expect(source.filter((event) => event.type === 'tool/call')).toEqual([call]);
    expect(source.filter((event) => event.type === 'plan/mode').map((event) => event.data.active)).toEqual([true, false]);
    expect(source.filter((event) => event.type === 'turn/start')).toEqual(held.filter((event) => event.type === 'turn/start'));
    const terminal = source.find((event) => event.type === 'turn/end')!;
    expect(terminal.data).toMatchObject({ turn: call.data.turn, reason: { kind: 'completed' } });
    const results = source.filter((event) => event.type === 'tool/result');
    expect(results).toHaveLength(1);
    expect(results[0]!.data).toMatchObject({ turn: call.data.turn });
    expect(results[0]!.data.error).toBeUndefined();
    const tool = results[0]!.data.message as Record<string, unknown>;
    expect(tool).toMatchObject({ role: 'tool', toolCallId: call.data.callId, isError: false });
    expect(results[0]!.seq).toBeGreaterThan(call.seq);
    expect(terminal.seq).toBeGreaterThan(results[0]!.seq);
    expect(expectConsumption(sessionId, turnId, clientMessageId, prompt)).toEqual(consumed);
    const inputs = source.filter((event) => event.type === 'user/message'
      && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user');
    expect(inputs).toHaveLength(1);
    expect(textBlocks(inputs[0]!.data.content)).toBe(prompt);
    const messages = source.filter((event) => event.type === 'assistant/message');
    expect(textBlocks((messages.at(-1)!.data.message as Record<string, unknown>).content)).toBe(receipt);
    const usage = messages.at(-1)!.data.usage;
    expect(usage).toEqual(expect.any(Object));
    const frames = framesForTurn(turnId).map((row) => row.payload as Record<string, unknown>);
    expect(frames.filter((frame) => frame.type === 'tool-input-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, toolName: 'exit_plan_mode', input: { plan } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, output: { content: tool.content, isError: false } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'error')).toEqual([]);
    expect(framesForTurn(turnId).filter((row) => (row.payload as Record<string, unknown>).type === 'data-result')).toEqual([
      expect.objectContaining({
        engine_kind: 'deepseek_harness',
        engine_turn_id: (inputs[0]!.data.source as Record<string, unknown>).rpcId,
        engine_output_cursor: expect.objectContaining({ sessionId: nativeSessionId, seq: terminal.seq }),
        payload: expect.objectContaining({ data: expect.objectContaining({ finish_reason: 'stop', usage }) }),
      }),
    ]);
    expect(frames.filter((frame) => frame.type === 'finish')).toEqual([
      // Parking ends the first UI stream segment; approval resumes this same turn.
      expect.objectContaining({ finishReason: 'tool-calls' }),
      expect.objectContaining({ finishReason: 'stop' }),
    ]);
    const history = (await api.getMessages(sessionId)).messages;
    const replies = history.filter((message) => message.role === 'assistant');
    expect(replies).toHaveLength(1);
    const reply = replies[0]!;
    expect(reply).toMatchObject({ turn_id: turnId, message_id: consumed.messageId });
    expect(messageProseBlocks(reply)).toEqual(messages.flatMap((event) =>
      textBlockValues((event.data.message as Record<string, unknown>).content)));
    expect((reply.blocks ?? []).filter((block) => block.type === 'tool_use')).toEqual([
      expect.objectContaining({ id: call.data.callId, name: 'exit_plan_mode', input: { plan } }),
    ]);
    const savedResults = (reply.blocks ?? []).filter((block) => block.type === 'tool_result');
    expect(savedResults).toHaveLength(1);
    expect(savedResults[0]!.tool_use_id).toBe(call.data.callId);
    expect(savedOutput(savedResults[0]!.content)).toEqual({ content: tool.content, isError: false });
    expect((reply.blocks ?? []).filter((block) => block.type === 'result')).toEqual([
      expect.objectContaining({ usage }),
    ]);
    const savedBlocks = JSON.stringify(reply.blocks);
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      const rendered = page.locator(`[data-message-id="${consumed.messageId}"]`);
      await expect(rendered).toContainText(receipt);
      await revealAssistantProcess(page, { within: rendered });
      const card = rendered.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
      await expect(card).toBeVisible();
      const trigger = card.locator('button[aria-expanded]').first();
      if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
      await expect(card).toContainText(textBlocks(tool.content));
      for (const line of plan.split('\n').filter(Boolean)) {
        await expect(card).toContainText(line.replace(/^(?:#|-)+\s+/, ''));
      }
      expect(JSON.stringify((await api.getMessages(sessionId)).messages
        .find((message) => message.message_id === consumed.messageId)!.blocks)).toBe(savedBlocks);
      await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    }
    evidence.completed = { native: source, frames, history };
  } finally {
    evidence.sessionId = sessionId;
    evidence.agentId = agentId;
    if (sessionId) for (const [key, read] of [
      ['nativeAtExit', () => nativeRows(sessionId)], ['journalAtExit', () => sessionEvents(sessionId)],
    ] as const) {
      try { evidence[key] = read(); }
      catch (error) { evidence[key] = { observationError: String(error) }; }
    }
    await info.attach('dsh-native-plan-detail-scene', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
