/** Every engine's approval reaches a person through the browser, in that engine's own words. */
import { expect, test, type Page } from '@playwright/test';

import {
  AstraApi,
  messageText,
  messageToolCalls,
  toolCallSettled,
  visibleMessages,
  type MessageRecord,
  type PendingInteraction,
  type ToolCallPart,
} from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import {
  sessionInputRows,
  sessionInteractionRows,
  sessionOutputRows,
  sessionResponseRows,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { dshApprovalCompleted, dshApprovalPanel, dshApprovalSource, dshPendingApproval, savedOutput } from '../fixtures/dshNativeApproval';
import type { DshApprovalEvidence } from '../fixtures/dshNativeApproval';
import { messageProseBlocks, textBlockValues, textBlocks } from '../fixtures/dshOutput';
import {
  decisionId,
  engineCases,
  engineProfileFor,
  modeName,
  profilesSupporting,
  profilesWithout,
  toolName,
  type EngineProfile,
} from '../fixtures/engineProfile';
import { trackSessions } from '../fixtures/sessionCleanup';
import { childToolEvidence, launchEvidence, nativeCalls, nativeRows } from '../fixtures/nativeChildLifecycle';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
const observations: unknown[] = [];
test.beforeEach(() => { sessionId = ''; observations.length = 0; });

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request, page }, info) => {
  if (!sessionId) return;
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const [session, pending, history, stream, inputs, responses, interactions, native] = await Promise.all([
    observe(() => api.getSession(sessionId)),
    observe(() => api.getPendingInteraction(sessionId)),
    observe(() => api.getMessages(sessionId, 50)),
    observe(() => sessionStreamBodies(page)),
    observe(() => sessionInputRows(sessionId)),
    observe(() => sessionResponseRows(sessionId)),
    observe(() => sessionInteractionRows(sessionId)),
    observe(() => nativeRows(sessionId)),
  ]);
  await info.attach('engine-approval-scene', {
    body: JSON.stringify({
      sessionId, observations, session, pending, history, stream, inputs, responses, interactions, native,
    }),
    contentType: 'application/json',
  });
});

/**
 * An absolute path inside this session's workspace.
 *
 * The engine is asked to write somewhere its sandbox policy actually covers.
 * A relative name is resolved against the process's own directory, where a
 * gated engine can simply run the write, fail on a read-only filesystem, and
 * report that instead of asking — which reads exactly like a product that
 * never raised the approval.
 */
async function workspacePath(api: AstraApi, session: string, name: string): Promise<string> {
  const detail = record(await api.adminSessionDetail(session));
  const identity = record(detail.runtime_identity);
  const root = String(identity.workspace_dir || '').replace(/\/+$/, '');
  expect(root, `session ${session} reports no workspace_dir: ${JSON.stringify(identity)}`)
    .not.toBe('');
  return `${root}/${name}`;
}

async function expectNoResponseFailure(
  page: Page,
  responseId: string,
  phase: 'live' | 'idle' = 'live',
): Promise<void> {
  const bodies = await sessionStreamBodies(page);
  observations.push({ browserBodies: bodies });
  const browserFrames = bodies.flatMap((body) => body.text
    .slice(0, body.text.lastIndexOf('\n') + 1).split('\n').flatMap((line) => {
      if (!line.startsWith('data:')) return [];
      const value = line.slice(5).trim();
      return value && value !== '[DONE]' ? [record(JSON.parse(value))] : [];
    }));
  if (phase === 'live') {
    expect(browserFrames.length, 'the real browser must receive the Session stream').toBeGreaterThan(0);
  }
  expect(browserFrames.filter((frame) => frame.type === 'error'),
    'a failure must not flash and disappear before the final DOM assertion').toEqual([]);
  expect(sessionResponseRows(sessionId).filter((row) => row.outcome === 'failed'),
    'no response of this conversation may be recorded as failed').toEqual([]);
  expect(sessionOutputRows(sessionId, responseId).map(({ part }) => part).filter((part) => part.type === 'error'),
    'the response must not record an error part').toEqual([]);
  await expect(page.getByTestId('run-view').getByText(/本轮执行失败|This turn failed/i)).toHaveCount(0);
}

/**
 * The response as the session's reader recorded its end: ended with
 * `outcome`, its last relayed part a `finish`, and no `error` part on the way.
 */
async function expectRecordedEnd(
  responseId: string,
  outcome: string,
  timeoutMs: number,
): Promise<SessionResponseRow> {
  let row: SessionResponseRow | undefined;
  await expect.poll(() => {
    row = sessionResponseRows(sessionId).find((candidate) => candidate.response_id === responseId);
    return row?.status;
  }, { timeout: timeoutMs, message: `response ${responseId} must be recorded as ended` }).toBe('ended');
  expect(row!.outcome, `response ${responseId} must end ${outcome}`).toBe(outcome);
  const parts = sessionOutputRows(sessionId, responseId).map(({ part }) => part);
  expect(parts.filter((part) => part.type === 'error'), 'the recorded end carries no error part').toEqual([]);
  expect(parts.at(-1)?.type, 'the recorded response ends on its own finish part').toBe('finish');
  return row!;
}

/** One tool call and the results it got, read off history or a child transcript. */
interface CommandCall {
  id: string;
  name: string;
  input: Record<string, unknown>;
  results: Array<{ isError: boolean; text: string }>;
}

/** Whether a tool part settled as a failure: an error or denied state, or an output the engine marked as an error. */
function callFailed(call: ToolCallPart): boolean {
  return call.state === 'output-error' || call.state === 'output-denied' || record(call.output).isError === true;
}

/** A response's tool calls, from the tool parts of its history message. */
function uiCommandCalls(message: MessageRecord | undefined): CommandCall[] {
  return (message ? messageToolCalls(message) : []).map((call) => ({
    id: call.toolCallId,
    name: call.toolName,
    input: record(call.input),
    results: toolCallSettled(call)
      ? [{ isError: callFailed(call), text: JSON.stringify(call.output ?? call.errorText ?? null) }]
      : [],
  }));
}

/** A child run's tool calls, from the content blocks of its transcript. */
function blockCommandCalls(blocks: Array<Record<string, unknown>>): CommandCall[] {
  return blocks.filter((block) => block.type === 'tool_use').map((block) => ({
    id: String(block.id ?? ''),
    name: String(block.name ?? ''),
    input: record(block.input),
    results: blocks.filter((result) => result.type === 'tool_result' && result.tool_use_id === block.id)
      .map((result) => ({
        isError: result.is_error === true,
        text: typeof result.content === 'string' ? result.content : JSON.stringify(result.content),
      })),
  }));
}

const commandCases = [
  ...engineCases().map((profile) => ({ profile, delegated: false })),
  ...engineCases().filter((profile) => profile.engine_kind === 'codex')
    .map((profile) => ({ profile, delegated: true })),
];
for (const { profile: declared, delegated } of commandCases) {
  const engine = declared.engine_kind;
  const title = delegated
    ? 'codex child command approval preserves its native tool owner without a parent command'
    : `${engine} first command preserves approval ownership and never flashes a turn failure`;
  test(title, async ({ page, request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const marker = `FIRST_COMMAND_${engine}_${Date.now()}`;
    const filename = `${marker}.txt`;
    sessionId = (await api.startConversation(profile.agent_id)).session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    test.info().annotations.push({ type: 'engine_kind', description: engine });
    await api.waitForSessionReady(sessionId);
    const approval = profile.contracts.tool_approval === true;
    if (approval) await api.setPermissionMode(sessionId, modeName(profile, delegated ? 'alternate' : 'gated'));
    const target = await workspacePath(api, sessionId, filename);
    const command = `printf %s '${marker}' > '${target}'`;
    const gate = { marker, started: target, release: target, completed: target };
    await api.uploadFileText(sessionId, target.slice(0, target.lastIndexOf('/')), filename, 'NOT_EXECUTED');
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);
    const commandPrompt = [
      engine === 'deepseek_harness'
        ? 'Use bash for one command, following its native denial-then-escalation flow.'
        : `Use your ${toolName(profile, 'command')} shell command tool exactly once.`,
      `Execute exactly this command: ${command}`,
      'This must be your first tool call. Do not inspect files, delegate, or use a file-editing tool.',
      'If this command needs permission, request execution approval and wait for my decision.',
      ...(engine === 'codex' ? [
        'Use exec_command with sandbox_permissions="require_escalated" and justification="Run the requested approval verification command" on the initial call, not after a failed unapproved attempt.',
      ] : []),
      ...(engine === 'deepseek_harness' ? [
        'First call bash without escalation. After its read-only sandbox denial, retry exactly the same command once with sandbox_permissions="workspace-write" and a justification based on that denial, then wait for my approval. Do not make any other tool calls.',
      ] : []),
      'After the command succeeds, briefly confirm completion without calling more tools.',
    ].join('\n');
    const prompt = delegated ? [
      `Delegate work item ${marker} to exactly one native child Agent.`,
      `Call spawn_agent exactly once with message=${JSON.stringify(commandPrompt)}.`,
      'Then call wait_agent for that Agent and wait for its completed result before answering.',
      'Do not run shell commands yourself. The child owns the complete workload.',
      'When the child finishes, briefly report its completion.',
    ].join('\n') : commandPrompt;
    const delivery = await sendPrompt(page, sessionId, prompt);
    let dshHeld: DshApprovalEvidence | undefined;
    const first = await api.waitForSession(sessionId, (detail) => {
      if (detail.last_response?.outcome === 'failed' || detail.last_error) {
        throw new Error(`first command failed before approval: ${JSON.stringify(detail)}`);
      }
      return Boolean(detail.pending_interaction || detail.last_response);
    }, 60_000);
    const pending = first.pending_interaction;
    const responseId = String(pending?.response_id || first.last_response?.response_id || '');
    expect(responseId).not.toBe('');
    observations.push({ engine, command, first });
    const panel = page.getByTestId('pending-interaction-panel');
    if (approval) {
      expect(pending, 'the first shell command must require approval, not settle without it').toBeTruthy();
      const interaction = pending!;
      expect(interaction.tool_name).toBe(toolName(profile, 'command'));
      expect(String(interaction.tool_call_id || '')).not.toBe('');
      if (engine === 'codex') {
        const raw = record(interaction.raw_input);
        expect(String(raw.itemId || '')).not.toBe('');
        expect(interaction.tool_call_id, 'native itemId is the tool identity, not the approval callback id')
          .toBe(raw.itemId);
        if (delegated) {
          await expect.poll(() => launchEvidence(sessionId, profile, gate), { timeout: 15_000 })
            .toHaveLength(1);
          const launches = launchEvidence(sessionId, profile, gate);
          expect(launches, 'the approval must come from a real native child').toHaveLength(1);
          await expect.poll(() => childToolEvidence(sessionId, profile, gate, launches[0]!.subpath), {
            timeout: 15_000,
          }).toHaveLength(1);
          const native = childToolEvidence(sessionId, profile, gate, launches[0]!.subpath);
          expect(native).toHaveLength(1);
          expect(native[0]!.id).toBe(raw.itemId);
          expect(native[0]!.nativeThreadId).toBe(raw.threadId);
          expect(native[0]!.commandArguments).toMatchObject({
            cmd: command, sandbox_permissions: 'require_escalated',
          });
          observations.push({ launches, native });
        }
      }
      await expect(panel).toBeVisible();
      await expect(page.getByTestId('session-conversation-shell'))
        .toHaveAttribute('data-pending-tool-call-id', String(interaction.tool_call_id));
      const before = await api.getMessages(sessionId, 100);
      expect(record(before.pending_interaction)).toMatchObject({
        interaction_id: interaction.interaction_id, tool_call_id: interaction.tool_call_id, response_id: responseId,
      });
      if (engine === 'deepseek_harness') {
        // The supplier requires a real sandbox denial before escalation.
        const attempts = uiCommandCalls(visibleMessages(before).find((message) => message.id === responseId));
        expect(attempts).toHaveLength(2);
        expect(attempts[0]!.id).not.toBe(attempts[1]!.id);
        expect(attempts[0]!.input.command).toBe(command);
        expect(attempts[0]!.input.sandbox_permissions).toBeUndefined();
        expect(attempts[1]!.input).toMatchObject({ command, sandbox_permissions: 'workspace-write' });
        expect(String(attempts[1]!.input.justification || '')).not.toBe('');
        expect(interaction.tool_call_id).toBe(attempts[1]!.id);
        expect(record(interaction.raw_input).callId).toBe(attempts[1]!.id);
        expect(attempts[0]!.results).toHaveLength(1);
        expect(attempts[0]!.results[0]!.text).toContain('[sandbox: file access denied under read-only mode]');
        expect(attempts[1]!.results, 'the held escalation has no result before the decision').toEqual([]);
        dshHeld = await dshPendingApproval(sessionId, interaction, prompt,
          String(delivery.request().postDataJSON().client_message_id || ''));
        observations.push({ dshHeld });
        await dshApprovalPanel(page, interaction);
      }
      expect(await api.downloadFileText(sessionId, filename), 'approval must precede the command side effect')
        .toBe('NOT_EXECUTED');
      await expectNoResponseFailure(page, responseId);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(panel).toBeVisible();
      expect(await api.getPendingInteraction(sessionId)).toMatchObject({
        interaction_id: interaction.interaction_id, tool_call_id: interaction.tool_call_id, response_id: responseId,
      });
      await expect(page.getByTestId('session-conversation-shell'))
        .toHaveAttribute('data-pending-tool-call-id', String(interaction.tool_call_id));
      if (engine === 'deepseek_harness') {
        expect(await api.getPendingInteraction(sessionId)).toMatchObject({ raw_input: interaction.raw_input });
        await dshApprovalPanel(page, interaction);
      }
      if (profile.presentation === 'decision') {
        const approve = decisionId(profile, 'approve');
        await panel.getByTestId(`decision-option-${approve}`).click();
        await browserAnswer(page, interaction, approve, () => panel.getByTestId('decision-submit').click());
      } else {
        await browserAnswer(page, interaction, 'approve', () => panel.getByRole('button', {
          name: /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/,
        }).last().click());
      }
    } else {
      // Pi has no supplier approval protocol: exercise its real command without inventing one.
      expect(pending).toBeFalsy();
      await expect(panel).toHaveCount(0);
    }
    const settled = await api.waitForSession(sessionId, (detail) => {
      if (detail.last_response?.outcome === 'failed' || detail.last_error) {
        throw new Error(`first command failed after approval: ${JSON.stringify(detail)}`);
      }
      return detail.last_response?.response_id === responseId && detail.last_response.outcome === 'completed'
        && detail.state === 'READY' && (detail.current_responses ?? []).length === 0 && !detail.pending_interaction;
    }, 60_000);
    expect(settled.last_response?.outcome).toBe('completed');
    await expectRecordedEnd(responseId, 'completed', 15_000);
    expect(await api.downloadFileText(sessionId, filename)).toBe(marker);
    const history = await api.getMessages(sessionId, 100);
    let calls = uiCommandCalls(visibleMessages(history).find((message) => message.id === responseId));
    if (delegated) {
      expect(calls.filter((call) => call.name === 'commandExecution'),
        'the parent must not execute a command that could supply the child approval ID').toEqual([]);
      const children = (await api.listChildRuns(sessionId)).child_runs;
      expect(children).toHaveLength(1);
      calls = blockCommandCalls((await api.getChildRunMessages(sessionId, children[0]!.child_run_id)).messages
        .flatMap((message) => message.content));
      const launches = launchEvidence(sessionId, profile, gate);
      const native = childToolEvidence(sessionId, profile, gate, launches[0]!.subpath);
      expect(native).toHaveLength(1);
      expect(native[0]!.id).toBe(pending!.tool_call_id);
      expect(native[0]!.result?.isError, 'the approved native child command must succeed').toBe(false);
    }
    const expectedCalls = engine === 'deepseek_harness' ? 2 : 1;
    expect(calls, 'only the supplier-required command attempts may precede completion').toHaveLength(expectedCalls);
    for (const call of calls) expect(call.name).toBe(toolName(profile, 'command'));
    const approvedCall = calls.at(-1)!;
    if (pending) expect(approvedCall.id).toBe(pending.tool_call_id);
    if (!delegated) {
      for (const call of calls) {
        const sourceCalls = () => {
          const native = nativeRows(sessionId).flatMap((row) =>
            nativeCalls(JSON.parse(String(row.entry_json)), row.subpath))
            .filter((nativeCall) => nativeCall.id === call.id);
          return [...new Map(native.map((call) => [call.id, call])).values()];
        };
        await expect.poll(sourceCalls, { timeout: 15_000 }).toHaveLength(1);
        const source = sourceCalls()[0]!;
        expect(source.name).toBe(engine === 'codex' ? 'exec_command' : toolName(profile, 'command'));
        expect(source.input[engine === 'codex' ? 'cmd' : 'command']).toBe(command);
        if (engine === 'deepseek_harness') {
          expect(source.input.sandbox_permissions).toBe(call.id === approvedCall.id ? 'workspace-write' : undefined);
        }
        observations.push({ source });
      }
    }
    if (engine === 'deepseek_harness') {
      const denial = calls[0]!.results;
      expect(denial).toHaveLength(1);
      expect(denial[0]!.text).toContain('[sandbox: file access denied under read-only mode]');
    }
    const results = approvedCall.results;
    expect(results).toHaveLength(1);
    expect(results[0]!.isError, 'the approved command must succeed').not.toBe(true);
    if (engine === 'deepseek_harness') {
      expect(dshHeld).toBeDefined();
      observations.push(await dshApprovalCompleted(api, sessionId, pending!, dshHeld!, 'allowed-once'));
    }
    await expect(panel).toHaveCount(0);
    await expectNoResponseFailure(page, responseId);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(panel).toHaveCount(0);
    expect((await api.getSession(sessionId)).last_response).toMatchObject({
      response_id: responseId, outcome: 'completed',
    });
    expect(await api.downloadFileText(sessionId, filename)).toBe(marker);
    await expectNoResponseFailure(page, responseId, 'idle');
  });
}

test('DSH rejects native command escalation without a write and remains usable', async ({ page, request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const marker = `REJECT_DSH_${Date.now()}`;
  const receipt = `${marker}_DONE`;
  const filename = `${marker}.txt`;
  sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, modeName(profile, 'gated'));
  const target = await workspacePath(api, sessionId, filename);
  const command = `printf %s '${marker}' > '${target}'`;
  await api.uploadFileText(sessionId, target.slice(0, target.lastIndexOf('/')), filename, 'NOT_EXECUTED');
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const prompt = [
    'Use bash following its native denial-then-escalation flow.',
    `First execute exactly this command without escalation: ${command}`,
    'After its read-only sandbox denial, retry exactly the same command once with sandbox_permissions="workspace-write" and a justification based on that denial, then wait for my approval.',
    `After my decision, reply exactly ${receipt}. If I reject it, do not retry, work around the denial, delegate or call another tool.`,
    'Do not make any other tool calls.',
  ].join('\n');
  const delivery = await sendPrompt(page, sessionId, prompt);
  const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 60_000);
  expect(pending, 'the real escalation must wait for a decision').not.toBeNull();
  const interaction = pending!;
  const responseId = String(interaction.response_id || '');
  expect(responseId).not.toBe('');
  const held = await dshPendingApproval(sessionId, interaction, prompt,
    String(delivery.request().postDataJSON().client_message_id || ''));
  const nativeCalls = dshApprovalSource(sessionId).filter((event) => event.type === 'tool/call');
  expect(nativeCalls).toHaveLength(2);
  expect(nativeCalls[0]!.data.name).toBe('bash');
  expect(JSON.parse(String(nativeCalls[0]!.data.arguments))).toMatchObject({ command });
  expect(JSON.parse(String(nativeCalls[0]!.data.arguments)).sandbox_permissions).toBeUndefined();
  expect(nativeCalls[1]).toEqual(held.call);
  expect(JSON.parse(String(held.call.data.arguments))).toMatchObject({ command, sandbox_permissions: 'workspace-write' });
  expect(nativeCalls[0]!.data.callId).not.toBe(held.call.data.callId);
  expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  observations.push({ prompt, command, pending: interaction, held, nativeCalls });
  const panel = page.getByTestId('pending-interaction-panel');
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    expect(await api.getPendingInteraction(sessionId)).toMatchObject({
      interaction_id: interaction.interaction_id, response_id: responseId,
      tool_call_id: held.call.data.callId, raw_input: interaction.raw_input,
    });
    await dshApprovalPanel(page, interaction);
    expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  }
  await browserAnswer(page, interaction, 'reject', () => panel.getByRole('button', {
    name: /Reject this action|拒绝此次操作/,
  }).click());
  const settled = await api.waitForSession(sessionId, (session) => {
    if (session.last_response?.outcome === 'failed' || session.last_error) {
      throw new Error(`rejected command poisoned its conversation: ${JSON.stringify(session)}`);
    }
    return session.last_response?.response_id === responseId && session.state === 'READY'
      && (session.current_responses ?? []).length === 0 && !session.pending_interaction;
  }, 60_000);
  expect(settled.last_response?.outcome, 'a rejection is answered, and the response completes').toBe('completed');
  await expectRecordedEnd(responseId, 'completed', 15_000);
  const completed = await dshApprovalCompleted(api, sessionId, interaction, held, 'rejected');
  const source = dshApprovalSource(sessionId);
  expect(source.filter((event) => event.type === 'tool/call')).toEqual(nativeCalls);
  const result = source.find((event) => event.type === 'tool/result'
    && record(event.data.message).toolCallId === held.call.data.callId)!;
  const rejection = 'Error: the user rejected escalating this command to "workspace-write"; '
    + 'it stays denied, so stop and explain instead of working around it';
  expect(textBlocks(record(result.data.message).content)).toBe(rejection);
  expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  observations.push({ completed });
  const stored = (await api.getMessages(sessionId)).messages.find((message) => message.id === responseId)!;
  expect(messageText(stored)).toContain(receipt);
  const parts = JSON.stringify(stored.parts);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    await expect(panel).toHaveCount(0);
    const reply = page.locator(`[data-message-id="${responseId}"]`);
    await expect(reply).toContainText(receipt);
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    const card = reply.locator(`[data-tool-call-id="${String(held.call.data.callId)}"]`);
    await expect(card).toBeVisible();
    const trigger = card.locator('button[aria-expanded]').first();
    if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
    const output = card.getByRole('heading', { name: 'Result', exact: true })
      .locator('..').locator('[data-language="json"]');
    await output.scrollIntoViewIfNeeded();
    await expect(output).toBeInViewport();
    expect(savedOutput(await output.innerText())).toEqual({
      content: record(result.data.message).content, isError: true,
    });
    await expectNoResponseFailure(page, responseId, reload ? 'idle' : 'live');
    expect(JSON.stringify((await api.getMessages(sessionId)).messages
      .find((message) => message.id === responseId)!.parts)).toBe(parts);
    expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  }
  const nextReceipt = `AFTER_${marker}`;
  const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
  await sendPrompt(page, sessionId, nextPrompt);
  const next = await api.waitForAssistantMessageMatching(sessionId, 0, (message) => messageText(message) === nextReceipt);
  await api.waitForSessionReady(sessionId);
  expect(next.id, 'the next message gets a response of its own').not.toBe(responseId);
  await expect.poll(() => dshApprovalSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(2);
  expect(dshApprovalSource(sessionId).filter((event) => event.type === 'tool/call')).toEqual(nativeCalls);
  expect(JSON.stringify((await api.getMessages(sessionId)).messages
    .find((message) => message.id === responseId)!.parts)).toBe(parts);
  await expect(page.locator(`[data-message-id="${next.id}"]`)).toContainText(nextReceipt);
  await page.reload();
  await expect(page.locator(`[data-message-id="${next.id}"]`)).toContainText(nextReceipt);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  observations.push({ next, native: dshApprovalSource(sessionId), history: await api.getMessages(sessionId) });
});

test('DSH stops a parked native approval and answers the next message on its own turn', async ({ page, request }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  const marker = `STOP_DSH_${Date.now()}`;
  const filename = `${marker}.txt`;
  sessionId = (await api.startConversation(profile.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, modeName(profile, 'gated'));
  const target = await workspacePath(api, sessionId, filename);
  const command = `printf %s '${marker}' > '${target}'`;
  await api.uploadFileText(sessionId, target.slice(0, target.lastIndexOf('/')), filename, 'NOT_EXECUTED');
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const prompt = [
    'Use bash following its native denial-then-escalation flow.',
    `First execute exactly this command without escalation: ${command}`,
    'After its read-only sandbox denial, retry exactly the same command once with sandbox_permissions="workspace-write" and a justification based on that denial, then wait for my approval.',
    'Do not make any other tool calls, retry again, work around the denial or delegate.',
  ].join('\n');
  const delivery = await sendPrompt(page, sessionId, prompt);
  const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 60_000);
  expect(pending, 'the real escalation must park before the user stops it').not.toBeNull();
  const interaction = pending!;
  const responseId = String(interaction.response_id || '');
  expect(responseId).not.toBe('');
  const held = await dshPendingApproval(sessionId, interaction, prompt,
    String(delivery.request().postDataJSON().client_message_id || ''));
  const calls = dshApprovalSource(sessionId).filter((event) => event.type === 'tool/call');
  expect(calls).toHaveLength(2);
  expect(calls[0]!.data.name).toBe('bash');
  expect(JSON.parse(String(calls[0]!.data.arguments))).toMatchObject({ command });
  expect(JSON.parse(String(calls[0]!.data.arguments)).sandbox_permissions).toBeUndefined();
  expect(calls[1]).toEqual(held.call);
  expect(JSON.parse(String(held.call.data.arguments))).toMatchObject({ command, sandbox_permissions: 'workspace-write' });
  expect(calls[0]!.data.callId).not.toBe(held.call.data.callId);
  expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  observations.push({ prompt, command, pending: interaction, held, calls });
  const panel = page.getByTestId('pending-interaction-panel');
  await dshApprovalPanel(page, interaction);
  const response = page.waitForResponse((value) => value.request().method() === 'POST'
    && new URL(value.url()).pathname.endsWith(`/sessions/${sessionId}/interrupt`));
  await panel.getByRole('button', { name: /^(Stop generating|Stopping|停止生成|停止中)$/i }).click();
  const interrupted = await response;
  expect(interrupted.status()).toBe(200);
  expect((await interrupted.json()).data).toMatchObject({ session_id: sessionId, status: 'accepted' });
  const settled = await api.waitForSession(sessionId, (session) => {
    if (session.last_response?.outcome === 'failed' || session.last_error) {
      throw new Error(`stopping an approval poisoned its conversation: ${JSON.stringify(session)}`);
    }
    return session.last_response?.response_id === responseId && session.state === 'READY'
      && (session.current_responses ?? []).length === 0 && !session.pending_interaction;
  }, 60_000);
  expect(settled.last_response?.outcome, 'a user stop ends the response as cancelled, never failed').toBe('cancelled');
  const ended = await expectRecordedEnd(responseId, 'cancelled', 15_000);
  // The harness's own word for how its turn ended, carried as it said it.
  expect(ended.native_reason).toBe('aborted');
  // Stop is not an answer: the approval closes with the response, unanswered.
  const stoppedApproval = sessionInteractionRows(sessionId)
    .filter((row) => row.interaction_id === interaction.interaction_id);
  expect(stoppedApproval, 'the approval the response waited on is recorded once').toHaveLength(1);
  expect(stoppedApproval[0]!.status, 'the stopped response leaves no approval open').not.toBe('open');
  expect(stoppedApproval[0]!.answer, 'a stop records no answer on the approval').toBeNull();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => dshApprovalSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const stoppedNative = dshApprovalSource(sessionId);
  const stoppedEnd = stoppedNative.find((event) => event.type === 'turn/end')!;
  expect(stoppedEnd.data).toMatchObject({ turn: held.call.data.turn, reason: { kind: 'aborted', reason: { kind: 'user' } } });
  expect(stoppedNative.filter((event) => event.type === 'approval/asked')).toEqual([held.asked]);
  expect(stoppedNative.filter((event) => event.type === 'approval/decided')).toEqual([
    expect.objectContaining({ data: { id: held.asked.data.id, outcome: 'cancelled' } }),
  ]);
  expect(stoppedNative.filter((event) => event.type === 'tool/call')).toEqual(calls);
  expect(stoppedNative.filter((event) => event.type === 'turn/start')).toHaveLength(1);
  expect(stoppedNative.filter((event) => event.type === 'user/message'
    && record(event.data.source).kind === 'user')).toEqual([held.input]);
  const nativeResults = stoppedNative.filter((event) => event.type === 'tool/result');
  expect(nativeResults).toHaveLength(calls.length);
  const stoppedOutput = sessionOutputRows(sessionId, responseId).map(({ part }) => part);
  expect(stoppedOutput.filter((part) => part.type === 'error')).toEqual([]);
  const finishReasons = stoppedOutput.filter((part) => part.type === 'finish').map((part) => part.finishReason);
  expect(finishReasons, 'the response paused once at the approval and ended once at the stop').toHaveLength(2);
  expect(finishReasons[0]).toBe('tool-calls');
  expect(finishReasons[1], 'the stop ends the response rather than pausing it again').not.toBe('tool-calls');
  const stopped = (await api.getMessages(sessionId)).messages.filter((message) => message.role === 'assistant');
  expect(stopped.map((message) => message.id), 'the stopped response is the one assistant message').toEqual([responseId]);
  expect(stopped[0]!.metadata?.outcome).toBe('cancelled');
  expect(messageProseBlocks(stopped[0]!)).toEqual(stoppedNative.filter((event) => event.type === 'assistant/message')
    .flatMap((event) => textBlockValues(record(event.data.message).content)));
  const stoppedCalls = messageToolCalls(stopped[0]!);
  expect(stoppedCalls.map((call) => call.toolCallId))
    .toEqual(calls.map((event) => event.data.callId));
  expect(stoppedCalls.filter(toolCallSettled).map((call) => call.toolCallId))
    .toEqual(calls.map((event) => event.data.callId));
  for (const call of calls) {
    const results = nativeResults.filter((event) => record(event.data.message).toolCallId === call.data.callId);
    expect(results).toHaveLength(1);
    const result = results[0]!;
    expect(result.data.turn).toBe(call.data.turn);
    expect(result.seq).toBeGreaterThan(call.seq);
    expect(result.seq).toBeLessThan(stoppedEnd.seq);
    const cancelledCall = call.data.callId === held.call.data.callId;
    expect(record(result.data.message)).toMatchObject({ role: 'tool', isError: cancelledCall });
    expect(textBlocks(record(result.data.message).content)).not.toBe('');
    expect(stoppedCalls.filter((stored) => stored.toolCallId === call.data.callId)).toEqual([
      expect.objectContaining({ toolName: call.data.name, input: JSON.parse(String(call.data.arguments)) }),
    ]);
    const stored = stoppedCalls.find((part) => part.toolCallId === call.data.callId)!;
    // The part carries the harness result as the harness recorded it: the
    // stopped call as an error, the sandbox denial before it as an ordinary result.
    expect(stored.output).toEqual({ content: record(result.data.message).content, isError: cancelledCall });
    if (!cancelledCall) {
      expect(textBlocks(record(result.data.message).content))
        .toContain('[sandbox: file access denied under read-only mode]');
    }
  }
  const savedParts = JSON.stringify(stopped[0]!.parts);
  observations.push({ stoppedNative, stoppedOutput, stopped });
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const reply = page.locator(`[data-message-id="${responseId}"]`);
    await expect(reply.getByText(/^(This response stopped early\.|本次回复提前结束。)$/)).toBeVisible();
    await expect(reply.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page, { within: reply });
    for (const call of calls) {
      const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
      await expect(card).toBeVisible();
      const trigger = card.locator('button[aria-expanded]').first();
      if (await trigger.getAttribute('aria-expanded') !== 'true') await trigger.click();
      await expect(card).toContainText(command);
    }
    await expectNoResponseFailure(page, responseId, reload ? 'idle' : 'live');
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    expect(JSON.stringify((await api.getMessages(sessionId)).messages
      .find((message) => message.id === responseId)!.parts)).toBe(savedParts);
    expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  }
  const nextReceipt = `AFTER_${marker}`;
  const nextPrompt = `Reply exactly ${nextReceipt}. Do not use tools or delegate.`;
  const nextDelivery = await sendPrompt(page, sessionId, nextPrompt);
  const nextClientMessageId = String(nextDelivery.request().postDataJSON().client_message_id || '');
  expect(nextClientMessageId).not.toBe('');
  const next = await api.waitForAssistantMessageMatching(sessionId, 0, (message) => messageText(message) === nextReceipt);
  await api.waitForSessionReady(sessionId);
  expect(next.id, 'the next message gets a response of its own').not.toBe(responseId);
  await expect.poll(() => dshApprovalSource(sessionId).filter((event) => event.type === 'turn/end')).toHaveLength(2);
  const source = dshApprovalSource(sessionId);
  expect(source.filter((event) => event.seq <= stoppedEnd.seq)).toEqual(stoppedNative);
  expect(source.filter((event) => event.type === 'tool/call')).toEqual(calls);
  const starts = source.filter((event) => event.type === 'turn/start');
  expect(starts).toHaveLength(2);
  expect(starts[1]!.seq).toBeGreaterThan(stoppedEnd.seq);
  expect(starts[1]!.data.turn).not.toBe(held.call.data.turn);
  const inputs = source.filter((event) => event.type === 'user/message' && record(event.data.source).kind === 'user');
  expect(inputs).toHaveLength(2);
  expect(inputs[0]).toEqual(held.input);
  expect(textBlocks(inputs[1]!.data.content)).toBe(nextPrompt);
  expect(inputs[1]!.seq).toBeGreaterThan(starts[1]!.seq);
  const nextNativeRpcId = String(record(inputs[1]!.data.source).rpcId || '');
  expect(nextNativeRpcId).not.toBe('');
  expect(nextNativeRpcId).not.toBe(record(held.input.data.source).rpcId);
  const terminal = source.filter((event) => event.type === 'turn/end')[1]!;
  expect(terminal.data).toMatchObject({ turn: starts[1]!.data.turn, reason: { kind: 'completed' } });
  const messages = source.filter((event) => event.type === 'assistant/message' && event.data.turn === starts[1]!.data.turn);
  expect(textBlocks(record(messages.at(-1)!.data.message).content)).toBe(nextReceipt);
  expect(messages.at(-1)!.data.usage).toEqual(expect.any(Object));
  expect(messageProseBlocks(next)).toEqual(messages.flatMap((event) => textBlockValues(record(event.data.message).content)));
  // The platform's own record of the next message: one input, admitted, and
  // answered by the response the page shows.
  const nextInputs = sessionInputRows(sessionId).filter((row) => row.client_message_id === nextClientMessageId);
  expect(nextInputs).toEqual([expect.objectContaining({
    content: nextPrompt, state: 'admitted', response_id: next.id,
  })]);
  const nextInputId = nextInputs[0]!.input_id;
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === next.id)).toEqual([
    expect.objectContaining({
      origin: 'input', input_ids: [nextInputId], status: 'ended', outcome: 'completed',
      native_reason: 'completed', usage: messages.at(-1)!.data.usage,
    }),
  ]);
  const nextOutput = sessionOutputRows(sessionId, next.id).map(({ part }) => part);
  expect(nextOutput.filter((part) => part.type === 'start').map((part) => record(part.messageMetadata).input_ids))
    .toEqual([[nextInputId]]);
  expect(nextOutput.filter((part) => part.type === 'finish')).toEqual([expect.objectContaining({ finishReason: 'stop' })]);
  expect(nextOutput.filter((part) => part.type === 'error' || String(part.type).startsWith('tool-'))).toEqual([]);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    await expect(page.locator(`[data-message-id="${next.id}"]`)).toContainText(nextReceipt);
    await expect(page.locator(`[data-message-id="${responseId}"]`)
      .getByText(/^(This response stopped early\.|本次回复提前结束。)$/)).toBeVisible();
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    const history = (await api.getMessages(sessionId)).messages;
    expect(history.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt, nextPrompt]);
    expect(history.filter((message) => message.role === 'assistant').map((message) => message.id))
      .toEqual([responseId, next.id]);
    expect(JSON.stringify(history
      .find((message) => message.id === responseId)!.parts)).toBe(savedParts);
    expect(await api.downloadFileText(sessionId, filename)).toBe('NOT_EXECUTED');
  }
  observations.push({ next, native: source, nextOutput, history: await api.getMessages(sessionId) });
});

async function browserAnswer(
  page: Page,
  pending: PendingInteraction,
  expectedDecision: string,
  click: () => Promise<void>,
): Promise<void> {
  // The path names the interaction, so matching it is what proves the browser
  // answered the exact pending native request.
  const answerPath = `/sessions/${sessionId}/interactions/${encodeURIComponent(String(pending.interaction_id))}/answer`;
  const response = page.waitForResponse((candidate) => (
    candidate.request().method() === 'POST'
    && new URL(candidate.url()).pathname.endsWith(answerPath)
  ));
  await click();
  const received = await response;
  // The id the engine will be answered with is the engine's own. A console that
  // sends a word the engine does not have is not a refusal it reports — the
  // tool simply never runs and the response ends with nothing said about why.
  // Read the shape the console actually sends rather than a shape this test
  // imagines: `answerInteraction` posts the answer nested under `answer`.
  // Asserting a flat shape would read `undefined` and pass for any value at
  // all had the comparison been written the other way round.
  const sent = record(JSON.parse(String(received.request().postData() || '{}')));
  const answered = record(sent.answer);
  expect(answered.decision, 'the browser must answer with the engine-declared id')
    .toBe(expectedDecision);
  expect(received.status(), 'the browser answer must be accepted by the real endpoint').toBe(200);
  const envelope = record(await received.json());
  const answer = record(envelope.data ?? envelope);
  expect(answer.interaction_id).toBe(pending.interaction_id);
  expect(answer.answered).toBe(true);
}

const approving = engineCases().filter((profile) => profile.contracts.tool_approval === true);

// Not a silent absence: an engine that declares no approval contract is the
// matrix saying the behaviour does not exist there, and the suite says so out
// loud so that "no test ran" can never be mistaken for "nothing to test".
test('engines that raise no approval are declared, not merely absent', async () => {
  const withoutApproval = profilesWithout('tool_approval').map((p) => p.engine_kind);
  const withApproval = profilesSupporting('tool_approval').map((p) => p.engine_kind);
  test.info().annotations.push({
    type: 'engines_without_tool_approval',
    description: withoutApproval.join(', ') || '(none)',
  });
  expect(withApproval.length, 'no engine declares tool_approval; the matrix carries nothing to drive')
    .toBeGreaterThan(0);
  for (const profile of profilesWithout('tool_approval')) {
    expect(
      profile.decisions ?? {},
      `${profile.engine_kind} declares no approval but still names decisions`,
    ).toEqual({});
  }
});

for (const declared of approving) {
  const engine = declared.engine_kind;

  test(`${engine} raises its approval in the browser and is answered in its own words`, async ({ page, request }) => {
    const profile = engineProfileFor(engine);
    const api = new AstraApi(request);
    const runId = Date.now();
    const marker = `APPROVAL_${engine.toUpperCase()}_${runId}`;
    sessionId = (await api.startConversation(profile.agent_id)).session_id;
    sessions.push(sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
    test.info().annotations.push({ type: 'engine_kind', description: engine });
    await api.waitForSessionReady(sessionId);

    // An engine asks before acting only while its own gated mode says it must.
    // The name is the engine's, read from the matrix and checked against what
    // the session reports, so a vocabulary change fails here rather than
    // quietly running the turn unattended.
    const gated = modeName(profile, 'gated');
    const capabilities = record(record(await api.getSession(sessionId)).engine_capabilities);
    const modes = (capabilities.permission_modes as string[] | undefined) ?? [];
    expect(modes, `${engine} must offer its gated mode ${gated}`).toContain(gated);
    await api.setPermissionMode(sessionId, gated);

    const target = await workspacePath(api, sessionId, `${marker}.txt`);
    await openSessionView(page, sessionId);
    // The phrasing the Python lane has proven across these engines: name the
    // engine's own write tool and an absolute workspace path, so the engine
    // escalates rather than attempting the write and reporting that it failed.
    await sendPrompt(page, sessionId, [
      `E2E ${engine} approval ${runId}.`,
      `Use the ${toolName(profile, 'write')} tool to create a file at ${target}`,
      `containing exactly ${marker}.`,
      'Do not use any other tool and do not run any other command.',
    ].join('\n'));

    const pending = await api.waitForPendingInteraction(sessionId, 90_000);
    observations.push({ engine, pending });
    // The backend half is covered by the Python lane; it is asserted here only
    // so a red in the browser half cannot be blamed on the adapter.
    expect(String(pending.presentation)).toBe(String(profile.presentation));

    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel).toBeVisible();

    if (profile.presentation === 'decision') {
      // The engine declares the choices and the console must know none of them
      // by name. A card that recognises only one engine's ids renders an empty
      // choice for every other, and holds the turn open with no way to answer.
      const options = (pending.options ?? []) as Array<Record<string, unknown>>;
      const optionIds = options.map((option) => String(option.id));
      expect(optionIds.length, `${engine} declared no decisions: ${JSON.stringify(pending)}`)
        .toBeGreaterThan(1);
      const offered = panel.locator('[data-testid^="decision-option-"]');
      await expect(
        offered,
        `the card must offer every decision ${engine} declared: ${optionIds.join(', ')}`,
      ).toHaveCount(optionIds.length);
      for (const optionId of optionIds) {
        await expect(
          panel.getByTestId(`decision-option-${optionId}`),
          `the card dropped ${engine}'s ${optionId} decision`,
        ).toBeVisible();
      }
      // The card as a person sees it, kept with the run. A count assertion
      // proves the options exist; only the image shows that the card is the
      // engine's own rather than another engine's copy wrapped around it.
      await test.info().attach(`${engine}-decision-card`, {
        body: await panel.screenshot(),
        contentType: 'image/png',
      });
      const approve = decisionId(profile, 'approve');
      expect(optionIds, `${engine} does not declare its approve id among its options`)
        .toContain(approve);
      await panel.getByTestId(`decision-option-${approve}`).click();
      await browserAnswer(page, pending, approve, () =>
        panel.getByTestId('decision-submit').click());
    } else {
      // The tool-approval presentation is the platform's own vocabulary: the
      // adapter translates `approve` into the engine's word on its own side,
      // so the browser sends the platform id and the card offers the shared
      // allow/reject controls rather than engine-declared options.
      await browserAnswer(page, pending, 'approve', () =>
        panel.getByRole('button', {
          name: /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/,
        }).last().click());
    }

    // Answering from the browser releases the response the approval was holding.
    await expect(panel).toHaveCount(0);
    // The platform's session vocabulary is upper case, as every sibling spec
    // reads it. A lower-case expectation here spent a whole testbed round
    // proving the turn had settled correctly.
    await expect
      .poll(async () => String(record(await api.getSession(sessionId)).state), { timeout: 120_000 })
      .toBe('READY');
  });
}
