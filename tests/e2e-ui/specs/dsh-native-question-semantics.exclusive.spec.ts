/** Native question answers resume their owning turn without another input. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

import {
  AstraApi, answersInput, messageText, messageToolCalls, type MessageRecord, type PendingInteraction,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import {
  sessionInputRows, sessionInteractionRows, sessionOutputRows, sessionResponseRows,
} from '../fixtures/dbOracle';
import { events, messageProseBlocks, textBlockValues, textBlocks } from '../fixtures/dshOutput';
import type { NativeEvent } from '../fixtures/dshOutput';
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

/**
 * The platform's record that the runtime took the page's one message into
 * `responseId`: the accepted input, admitted into that response, which names
 * it from its first part. Returns the input's id.
 */
function expectAdmission(
  sessionId: string, responseId: string, clientMessageId: string, prompt: string,
): string {
  const accepted = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(accepted).toEqual([expect.objectContaining({ content: prompt, state: 'admitted', response_id: responseId })]);
  const inputId = accepted[0]!.input_id;
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === responseId)).toEqual([
    expect.objectContaining({ origin: 'input', input_ids: [inputId] }),
  ]);
  expect(sessionOutputRows(sessionId, responseId).map((row) => row.part)[0]).toMatchObject({
    type: 'start', messageId: responseId,
    messageMetadata: { response_id: responseId, input_ids: [inputId], origin: 'input' },
  });
  return inputId;
}

/** The usage the harness last reported for one turn, from its attempts' settlements. */
function nativeTurnUsage(source: NativeEvent[], nativeTurn: unknown): unknown {
  const settled = source.filter((event) => (event.type === 'assistant/message' || event.type === 'assistant/attempt')
    && event.data.turn === nativeTurn && event.data.usage && typeof event.data.usage === 'object');
  return settled.at(-1)?.data.usage;
}

/** The input the page sent with `clientMessageId`, as the platform recorded it. */
function pageInputId(sessionId: string, clientMessageId: string): string {
  const rows = sessionInputRows(sessionId).filter((row) => row.client_message_id === clientMessageId);
  expect(rows, 'the page message is one accepted input').toHaveLength(1);
  return rows[0]!.input_id;
}

/** Whether a request is the console's answer to one interaction, read from its decoded path. */
function answersInteraction(url: string, sessionId: string, interactionId: string): boolean {
  return decodeURIComponent(new URL(url).pathname)
    === apiPath(`/sessions/${sessionId}/interactions/${interactionId}/answer`);
}

/**
 * One response's stored parts close its message at the interaction and reopen
 * the same message when the answer lets the response continue.
 */
function expectPausedAndResumed(frames: Record<string, unknown>[], responseId: string, interactionId: string): void {
  expect(frames.filter((frame) => frame.type === 'finish').map((frame) => frame.finishReason)).toEqual([
    'tool-calls', 'stop',
  ]);
  expect(frames.filter((frame) => frame.type === 'start').map((frame) => frame.messageId))
    .toEqual([responseId, responseId]);
  const interactions = frames.filter((frame) => frame.type === 'data-interaction');
  expect(interactions.map((frame) => (frame.data as Record<string, unknown>).interaction_id)).toEqual([interactionId]);
  const paused = frames.findIndex((frame) => frame.type === 'finish');
  expect(frames.indexOf(interactions[0]!)).toBe(paused - 1);
  expect(frames[paused + 1]).toMatchObject({ type: 'start', messageId: responseId });
  expect(frames.at(-1)).toMatchObject({ type: 'finish', finishReason: 'stop' });
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
      runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
    const responseId = String(interaction.response_id || '');
    expect(responseId, 'the interaction names the response it pauses').not.toBe('');
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
    const inputId = expectAdmission(sessionId, responseId, clientMessageId, prompt);
    expect(sessionResponseRows(sessionId).find((row) => row.response_id === responseId))
      .toMatchObject({ status: 'waiting', outcome: null });
    expect(sessionInteractionRows(sessionId)).toEqual([expect.objectContaining({
      interaction_id: interaction.interaction_id, response_id: responseId, status: 'open', answer: null,
    })]);
    evidence.parked = { pending: interaction, native: parked, inputId };

    for (const reload of [false, true]) {
      if (reload) await page.reload();
      await expect(page.getByTestId('pending-interaction-panel')).toBeVisible();
      const restored = await api.getPendingInteraction(sessionId);
      expect(restored).toMatchObject({ interaction_id: interaction.interaction_id, response_id: responseId,
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
      && answersInteraction(value.url(), sessionId, interaction.interaction_id));
    await panel.getByRole('button', { name: scenario.kind === 'decline'
      ? /Decline to answer|拒绝回答/ : /Submit answer|提交回答/ }).click();
    const answered = await response;
    expect(answered.status()).toBe(200);
    const body = answered.request().postDataJSON() as Record<string, unknown>;
    const answer = { interaction_id: interaction.interaction_id,
      ...(scenario.kind === 'decline' ? { decline: true } : { answers: expectedAnswers }) };
    expect(body).toEqual({ answer });
    expect((await answered.json()).data).toMatchObject({ interaction_id: interaction.interaction_id, answered: true });
    evidence.browserAnswer = body;
    let continued = await api.waitForReplyTo(sessionId, inputId);
    await expect.poll(async () => {
      continued = await api.waitForReplyTo(sessionId, inputId);
      return messageText(continued);
    }).toContain(receipt);
    await api.waitForSessionReady(sessionId);
    await expect(panel).toHaveCount(0);
    // The answer continued the response it paused; it is not another message.
    expect(continued.id).toBe(responseId);
    expect(sessionInputRows(sessionId).map((row) => row.content)).toEqual([prompt]);
    expect(sessionInteractionRows(sessionId)).toEqual([expect.objectContaining({
      interaction_id: interaction.interaction_id, response_id: responseId, status: 'answered', answer,
    })]);
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
    expect(expectAdmission(sessionId, responseId, clientMessageId, prompt)).toBe(inputId);
    const frames = sessionOutputRows(sessionId, responseId).map((row) => row.part);
    expect(frames.filter((frame) => frame.type === 'tool-input-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, toolName: 'ask_user_question', input: { questions } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId,
        output: { content: tool.content, isError: scenario.kind === 'decline' } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'error')).toEqual([]);
    expectPausedAndResumed(frames, responseId, interaction.interaction_id);
    const usage = nativeTurnUsage(source, call.data.turn);
    expect(usage).toEqual(expect.any(Object));
    expect(sessionResponseRows(sessionId)).toEqual([expect.objectContaining({ response_id: responseId,
      origin: 'input', status: 'ended', outcome: 'completed', native_reason: 'completed', input_ids: [inputId], usage,
    })]);
    const history = (await api.getMessages(sessionId)).messages;
    const retained = history.filter((message) => message.role === 'assistant');
    expect(retained.map((message) => message.id)).toEqual([responseId]);
    expect(answersInput(retained[0]!, inputId)).toBe(true);
    const nativeMessages = source.filter((event) => event.type === 'assistant/message');
    expect(messageProseBlocks(retained[0]!)).toEqual(nativeMessages.flatMap((event) =>
      textBlockValues((event.data.message as Record<string, unknown>).content)));
    expect(textBlocks((nativeMessages.at(-1)!.data.message as Record<string, unknown>).content)).toBe(receipt);
    expect(tool.content).toEqual([{ type: 'text', text: textBlocks(tool.content) }]);
    // History holds the call once, with the supplier's arguments and the exact
    // result it recorded.
    expect(messageToolCalls(retained[0]!)).toEqual([expect.objectContaining({
      toolCallId: call.data.callId, toolName: 'ask_user_question', input: JSON.parse(String(call.data.arguments)),
      state: 'output-available', output: { content: tool.content, isError: scenario.kind === 'decline' },
    })]);
    const storedParts = JSON.stringify(retained[0]!.parts);
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      const reply = page.locator(`[data-message-id="${responseId}"]`);
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
      const reread = (await api.getMessages(sessionId)).messages.find((message) => message.id === responseId);
      expect(JSON.stringify(reread!.parts)).toBe(storedParts);
    }
    evidence.completed = { native: source, frames, history };
    if (scenario.kind === 'decline') {
      const nextReceipt = `AFTER_DECLINE_${marker}`;
      const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
      const nextDelivery = await sendPrompt(page, sessionId, nextPrompt);
      const nextClientMessageId = String(nextDelivery.request().postDataJSON().client_message_id || '');
      expect(nextClientMessageId).not.toBe('');
      const next: MessageRecord = await api.waitForReplyTo(sessionId, pageInputId(sessionId, nextClientMessageId));
      expect(messageText(next)).toBe(nextReceipt);
      expect(next.metadata?.outcome).toBe('completed');
      await api.waitForSessionReady(sessionId);
      expect(next.id).not.toBe(responseId);
      expectAdmission(sessionId, next.id, nextClientMessageId, nextPrompt);
      expect(sessionInputRows(sessionId).map((row) => row.content)).toEqual([prompt, nextPrompt]);
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
      const nextFrames = sessionOutputRows(sessionId, next.id).map((row) => row.part);
      expect(nextFrames.filter((frame) => frame.type === 'error')).toEqual([]);
      expect(nextFrames.filter((frame) => frame.type === 'finish')).toEqual([
        expect.objectContaining({ finishReason: 'stop' }),
      ]);
      expect(sessionResponseRows(sessionId).find((row) => row.response_id === next.id))
        .toMatchObject({ status: 'ended', outcome: 'completed', native_reason: 'completed' });
      const laterHistory = (await api.getMessages(sessionId)).messages;
      expect(messageProseBlocks(laterHistory.find((message) => message.id === next.id)!))
        .toEqual([nextReceipt]);
      expect(JSON.stringify(laterHistory.find((message) => message.id === responseId)!.parts))
        .toBe(storedParts);
      const priorProcess = page.locator(`[data-message-id="${responseId}"]`)
        .getByTestId('assistant-turn-process-trigger');
      await priorProcess.click();
      await expect(priorProcess).toHaveAttribute('aria-expanded', 'false');
      for (const reload of [false, true]) {
        if (reload) await page.reload();
        await expect(page.locator(`[data-message-id="${next.id}"]`)).toContainText(nextReceipt);
        await expect(page.locator(`[data-message-id="${responseId}"]`)).toContainText(receipt);
        await expect(page.getByTestId('composer-prompt')).toBeEnabled();
      }
      evidence.next = { responseId: next.id, native: later, frames: nextFrames, history: laterHistory };
    }
  } finally {
    if (sessionId) {
      evidence.sessionId = sessionId;
      evidence.agentId = agentId;
      evidence.pending = pending;
      for (const [key, read] of [
        ['nativeAtExit', () => nativeRows(sessionId)],
        ['inputsAtExit', () => sessionInputRows(sessionId)],
        ['responsesAtExit', () => sessionResponseRows(sessionId)],
        ['interactionsAtExit', () => sessionInteractionRows(sessionId)],
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
      runtime_profile_name: profile.runtime_profile_name, model: profile.model, prepared_capacity: 0 })).agent_id;
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
    const responseId = String(interaction.response_id || '');
    expect(responseId, 'the interaction names the response it pauses').not.toBe('');
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
    const inputId = expectAdmission(sessionId, responseId, clientMessageId, prompt);
    expect(sessionResponseRows(sessionId).find((row) => row.response_id === responseId))
      .toMatchObject({ status: 'waiting', outcome: null });
    expect(sessionInteractionRows(sessionId)).toEqual([expect.objectContaining({
      interaction_id: interaction.interaction_id, response_id: responseId, status: 'open', answer: null,
    })]);
    evidence.held = { pending: interaction, native: held, inputId };
    const panel = page.getByTestId('pending-interaction-panel');
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      await expect(panel).toBeVisible();
      const restored = await api.getPendingInteraction(sessionId);
      expect(restored).toMatchObject({ interaction_id: interaction.interaction_id, response_id: responseId,
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
      && answersInteraction(value.url(), sessionId, interaction.interaction_id));
    await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
    const answered = await response;
    expect(answered.status()).toBe(200);
    const answer = { interaction_id: interaction.interaction_id,
      answers: [{ question_id: question.id, option_label: 'Approve' }] };
    expect(answered.request().postDataJSON()).toEqual({ answer });
    expect((await answered.json()).data).toMatchObject({ interaction_id: interaction.interaction_id, answered: true });
    const replied = await api.waitForReplyTo(sessionId, inputId);
    expect(messageText(replied)).toBe(receipt);
    await api.waitForSessionReady(sessionId);
    await expect(panel).toHaveCount(0);
    // The approval continued the reviewed response; it is not another message.
    expect(replied.id).toBe(responseId);
    expect(sessionInputRows(sessionId).map((row) => row.content)).toEqual([prompt]);
    expect(sessionInteractionRows(sessionId)).toEqual([expect.objectContaining({
      interaction_id: interaction.interaction_id, response_id: responseId, status: 'answered', answer,
    })]);
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
    expect(expectAdmission(sessionId, responseId, clientMessageId, prompt)).toBe(inputId);
    const inputs = source.filter((event) => event.type === 'user/message'
      && (event.data.source as Record<string, unknown> | undefined)?.kind === 'user');
    expect(inputs).toHaveLength(1);
    expect(textBlocks(inputs[0]!.data.content)).toBe(prompt);
    const messages = source.filter((event) => event.type === 'assistant/message');
    expect(textBlocks((messages.at(-1)!.data.message as Record<string, unknown>).content)).toBe(receipt);
    const usage = nativeTurnUsage(source, call.data.turn);
    expect(usage).toEqual(expect.any(Object));
    const frames = sessionOutputRows(sessionId, responseId).map((row) => row.part);
    expect(frames.filter((frame) => frame.type === 'tool-input-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, toolName: 'exit_plan_mode', input: { plan } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
      expect.objectContaining({ toolCallId: call.data.callId, output: { content: tool.content, isError: false } }),
    ]);
    expect(frames.filter((frame) => frame.type === 'error')).toEqual([]);
    // Parking ends the first message segment; approval resumes this same response.
    expectPausedAndResumed(frames, responseId, interaction.interaction_id);
    // The platform's record of the ending carries the supplier's own last usage.
    expect(sessionResponseRows(sessionId)).toEqual([expect.objectContaining({ response_id: responseId,
      origin: 'input', status: 'ended', outcome: 'completed', native_reason: 'completed', input_ids: [inputId], usage,
    })]);
    const history = (await api.getMessages(sessionId)).messages;
    const replies = history.filter((message) => message.role === 'assistant');
    expect(replies).toHaveLength(1);
    const reply = replies[0]!;
    expect(reply.id).toBe(responseId);
    expect(answersInput(reply, inputId)).toBe(true);
    expect(messageProseBlocks(reply)).toEqual(messages.flatMap((event) =>
      textBlockValues((event.data.message as Record<string, unknown>).content)));
    expect(messageToolCalls(reply)).toEqual([expect.objectContaining({
      toolCallId: call.data.callId, toolName: 'exit_plan_mode', input: { plan },
      state: 'output-available', output: { content: tool.content, isError: false },
    })]);
    const savedParts = JSON.stringify(reply.parts);
    for (const reload of [false, true]) {
      if (reload) await page.reload();
      const rendered = page.locator(`[data-message-id="${responseId}"]`);
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
        .find((message) => message.id === responseId)!.parts)).toBe(savedParts);
      await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    }
    evidence.completed = { native: source, frames, history };
  } finally {
    evidence.sessionId = sessionId;
    evidence.agentId = agentId;
    if (sessionId) for (const [key, read] of [
      ['nativeAtExit', () => nativeRows(sessionId)], ['inputsAtExit', () => sessionInputRows(sessionId)],
      ['responsesAtExit', () => sessionResponseRows(sessionId)],
      ['interactionsAtExit', () => sessionInteractionRows(sessionId)],
    ] as const) {
      try { evidence[key] = read(); }
      catch (error) { evidence[key] = { observationError: String(error) }; }
    }
    await info.attach('dsh-native-plan-detail-scene', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
