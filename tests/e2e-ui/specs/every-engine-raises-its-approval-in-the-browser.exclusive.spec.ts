/** Every engine's approval reaches a person through the browser, in that engine's own words. */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, visibleMessages, type PendingInteraction } from '../fixtures/astraApi';
import { framesForTurn, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
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
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

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
  const [session, pending, history, stream, events] = await Promise.all([
    observe(() => api.getSession(sessionId)),
    observe(() => api.getPendingInteraction(sessionId)),
    observe(() => api.getMessages(sessionId, 50)),
    observe(() => aiStreamBodies(page)),
    observe(() => sessionEvents(sessionId)),
  ]);
  await info.attach('engine-approval-scene', {
    body: JSON.stringify({ sessionId, observations, session, pending, history, stream, events }),
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

async function expectNoTurnFailure(page: Page, turnId: string, phase: 'live' | 'idle' = 'live'): Promise<void> {
  const bodies = await aiStreamBodies(page);
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
  expect(browserFrames.filter((frame) => ['error', 'data-turn-failure'].includes(String(frame.type))),
    'a failure must not flash and disappear before the final DOM assertion').toEqual([]);
  expect(sessionEvents(sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
  expect(framesForTurn(turnId).filter((event) =>
    ['error', 'data-turn-failure'].includes(String(record(event.payload).type)))).toEqual([]);
  await expect(page.getByTestId('run-view').getByText(
    /本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了|This turn failed/i,
  )).toHaveCount(0);
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
    await sendPrompt(page, sessionId, delegated ? [
      `Delegate this task to exactly one child using spawn_agent. Task label: ${marker}.`,
      'You, the parent, must not execute any command, inspect any file, or use any file-editing tool.',
      'Pass the following instructions verbatim to the child, then wait for that child to finish using wait_agent. Do not close the child.',
      commandPrompt,
      'When the child finishes, briefly report its completion. Do not perform the child task yourself.',
    ].join('\n') : commandPrompt);
    const first = await api.waitForSession(sessionId, (detail) => {
      if (detail.last_turn_status === 'FAILED' || detail.last_error) {
        throw new Error(`first command failed before approval: ${JSON.stringify(detail)}`);
      }
      return Boolean(detail.pending_interaction || detail.last_turn_id);
    }, 60_000);
    const pending = first.pending_interaction;
    const turnId = String(pending?.turn_id || first.last_turn_id || '');
    expect(turnId).not.toBe('');
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
        interaction_id: interaction.interaction_id, tool_call_id: interaction.tool_call_id, turn_id: turnId,
      });
      if (engine === 'deepseek_harness') {
        // dsh-tool-bash 0.1.5-rc.2 requires a real denial before escalation.
        const beforeBlocks = visibleMessages(before).filter((message) => message.turn_id === turnId)
          .flatMap((message) => message.blocks || []);
        const attempts = beforeBlocks.filter((block) => block.type === 'tool_use');
        expect(attempts).toHaveLength(2);
        expect(attempts[0]!.id).not.toBe(attempts[1]!.id);
        expect(record(attempts[0]!.input).command).toBe(command);
        expect(record(attempts[0]!.input).sandbox_permissions).toBeUndefined();
        expect(record(attempts[1]!.input)).toMatchObject({ command, sandbox_permissions: 'workspace-write' });
        expect(String(record(attempts[1]!.input).justification || '')).not.toBe('');
        expect(interaction.tool_call_id).toBe(attempts[1]!.id);
        expect(record(interaction.raw_input).callId).toBe(attempts[1]!.id);
        const denial = beforeBlocks.filter((block) =>
          block.type === 'tool_result' && block.tool_use_id === attempts[0]!.id);
        expect(denial).toHaveLength(1);
        expect(String(denial[0]!.content)).toContain('[sandbox: file access denied under read-only mode]');
        expect(beforeBlocks.filter((block) =>
          block.type === 'tool_result' && block.tool_use_id === attempts[1]!.id)).toEqual([]);
      }
      expect(await api.downloadFileText(sessionId, filename), 'approval must precede the command side effect')
        .toBe('NOT_EXECUTED');
      await expectNoTurnFailure(page, turnId);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(panel).toBeVisible();
      expect(await api.getPendingInteraction(sessionId)).toMatchObject({
        interaction_id: interaction.interaction_id, tool_call_id: interaction.tool_call_id, turn_id: turnId,
      });
      await expect(page.getByTestId('session-conversation-shell'))
        .toHaveAttribute('data-pending-tool-call-id', String(interaction.tool_call_id));
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
      if (detail.last_turn_status === 'FAILED' || detail.last_error) {
        throw new Error(`first command failed after approval: ${JSON.stringify(detail)}`);
      }
      return detail.last_turn_id === turnId && detail.last_turn_status === 'COMPLETED'
        && detail.state === 'READY' && !detail.current_turn_id && !detail.pending_interaction;
    }, 60_000);
    expect(settled.last_turn_status).toBe('COMPLETED');
    await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', 15_000);
    expect(await api.downloadFileText(sessionId, filename)).toBe(marker);
    const history = await api.getMessages(sessionId, 100);
    let blocks = visibleMessages(history).filter((message) => message.turn_id === turnId)
      .flatMap((message) => message.blocks || []);
    if (delegated) {
      expect(blocks.filter((block) => block.type === 'tool_use' && block.name === 'commandExecution'),
        'the parent must not execute a command that could supply the child approval ID').toEqual([]);
      const children = (await api.listChildRuns(sessionId)).child_runs;
      expect(children).toHaveLength(1);
      blocks = (await api.getChildRunMessages(sessionId, children[0]!.child_run_id)).messages
        .flatMap((message) => message.content);
      const launches = launchEvidence(sessionId, profile, gate);
      const native = childToolEvidence(sessionId, profile, gate, launches[0]!.subpath);
      expect(native).toHaveLength(1);
      expect(native[0]!.id).toBe(pending!.tool_call_id);
      expect(native[0]!.result?.isError, 'the approved native child command must succeed').toBe(false);
    }
    const calls = blocks.filter((block) => block.type === 'tool_use');
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
      const denial = blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === calls[0]!.id);
      expect(denial).toHaveLength(1);
      expect(String(denial[0]!.content)).toContain('[sandbox: file access denied under read-only mode]');
    }
    const results = blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === approvedCall.id);
    expect(results).toHaveLength(1);
    expect(results[0]!.is_error).not.toBe(true);
    await expect(panel).toHaveCount(0);
    await expectNoTurnFailure(page, turnId);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(panel).toHaveCount(0);
    expect((await api.getSession(sessionId)).last_turn_status).toBe('COMPLETED');
    expect(await api.downloadFileText(sessionId, filename)).toBe(marker);
    await expectNoTurnFailure(page, turnId, 'idle');
  });
}

async function browserAnswer(
  page: Page,
  pending: PendingInteraction,
  expectedDecision: string,
  click: () => Promise<void>,
): Promise<void> {
  const response = page.waitForResponse((candidate) => (
    candidate.request().method() === 'POST'
    && candidate.url().includes(`/sessions/${sessionId}/interaction-respond`)
  ));
  await click();
  const received = await response;
  // The id the engine will be answered with is the engine's own. A console that
  // sends a word the engine does not have is not a refusal it reports — the
  // tool simply never runs and the turn ends with nothing said about why.
  // Read the shape the console actually sends rather than a shape this test
  // imagines: `answerPendingInteraction` posts the answer nested under
  // `answer`, beside a top-level `interaction_id`. Asserting the flat shape
  // read `undefined` and would have passed for any value at all had the
  // comparison been written the other way round.
  const sent = record(JSON.parse(String(received.request().postData() || '{}')));
  expect(sent.interaction_id, 'the browser must answer the exact pending native request')
    .toBe(pending.interaction_id);
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

    // Answering from the browser releases the turn the approval was holding.
    await expect(panel).toHaveCount(0);
    // The platform's session vocabulary is upper case, as every sibling spec
    // reads it. A lower-case expectation here spent a whole testbed round
    // proving the turn had settled correctly.
    await expect
      .poll(async () => String(record(await api.getSession(sessionId)).state), { timeout: 120_000 })
      .toBe('READY');
  });
}
